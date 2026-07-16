"""Smoke tests for the HTTP surface.

Not exhaustive — these exist to catch the "a refactor silently broke a route"
class of regression before it reaches the browser. They run in FAKE mode against
an empty temp DB (see conftest.py).
"""

import io
import struct
import wave


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Vibedentify" in r.data


def test_map_empty_db(client):
    r = client.get("/map")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"nodes": [], "edges": []}


def test_tags_empty(client):
    r = client.get("/tags")
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)


def test_vibes_empty(client):
    r = client.get("/vibes")
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)


def test_similar_unknown_hash_404(client):
    r = client.get("/similar/deadbeefdeadbeef")
    assert r.status_code == 404


def test_save_training_requires_genre(client):
    r = client.post("/save_training", data={})
    assert r.status_code == 400


def test_audio_unknown_hash_404(client):
    r = client.get("/audio/deadbeef")
    assert r.status_code == 404


def _tiny_wav_bytes():
    buf = io.BytesIO()
    w = wave.open(buf, "w")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(struct.pack("<800h", *([0] * 800)))
    w.close()
    return buf.getvalue()


def test_analyze_fake_returns_full_payload(client):
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "probe.wav")}
    r = client.post("/analyze", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    for key in ("styles", "bpm", "hash", "waveform"):
        assert key in body, f"missing {key} in analyze payload"


def test_refine_fake(client):
    # exercises the FINE_HOP_SECONDS path (was an unimported-name bug after the split)
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "probe.wav")}
    r = client.post("/refine", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert "hop_seconds" in body and "segments" in body


def test_vibes_create_and_duplicate(client):
    # exercises the sqlite3.IntegrityError path (was an unimported-name bug)
    r1 = client.post("/vibes", json={"name": "Test Vibe"})
    assert r1.status_code == 200
    assert r1.get_json()["name"] == "Test Vibe"
    r2 = client.post("/vibes", json={"name": "Test Vibe"})
    assert r2.status_code == 409


def test_forget_deletes_track(client):
    # analyze a track, then forget it -> removed from the cache
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "gone.wav")}
    h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()["hash"]
    r1 = client.post(f"/forget/{h}")
    assert r1.status_code == 200
    assert r1.get_json()["deleted"] == 1
    # forgetting again is a harmless no-op (already gone)
    assert client.post(f"/forget/{h}").get_json()["deleted"] == 0


def test_guide_route_serves_markdown(client):
    r = client.get("/guide")
    assert r.status_code == 200
    assert b"User Guide" in r.data


def test_audit_route_returns_list(client):
    r = client.get("/audit")
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)  # empty on the throwaway DB


def test_misread_flag_logic():
    # the core rule: a shaky read whose close neighbours agree on a different
    # family gets flagged; a confident read does not.
    from vibedentify import insight

    close_bass = [(0.90, "Dubstep"), (0.88, "Dubstep"), (0.87, "Drum n Bass")]
    flagged = insight._score("K-Pop", 0.29, close_bass)
    assert flagged["flag"] is True
    assert flagged["suggested_family"] == insight.family_of("Dubstep")

    confident = insight._score("K-Pop", 0.80, close_bass)
    assert confident["flag"] is False
