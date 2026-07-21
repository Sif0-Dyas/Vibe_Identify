"""Smoke tests for the HTTP surface.

Not exhaustive — these exist to catch the "a refactor silently broke a route"
class of regression before it reaches the browser. They run in FAKE mode against
an empty temp DB (see conftest.py).
"""

import io
import json
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


def _tiny_wav_bytes(sample=0):
    # `sample` varies the audio content so callers can make DISTINCT files
    # (distinct content hash). A batch/map test needs separate tracks, not three
    # byte-identical copies the content-hash cache would collapse into one.
    buf = io.BytesIO()
    w = wave.open(buf, "w")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(struct.pack("<800h", *([sample] * 800)))
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


def test_override_sets_genre(client):
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "ov.wav")}
    h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()["hash"]
    r = client.post(f"/override/{h}", json={"genre": "Riddim"})
    assert r.status_code == 200
    assert r.get_json()["genre"] == "Riddim"
    # the override wins as the dominant style -> shows on the map
    node = next(n for n in client.get("/map").get_json()["nodes"] if n["hash"] == h)
    assert node["style"] == "Riddim"
    assert client.post(f"/override/{h}", json={}).status_code == 400  # empty rejected


def test_guide_route_serves_markdown(client):
    r = client.get("/guide")
    assert r.status_code == 200
    assert b"User Guide" in r.data


def test_audit_route_returns_list(client):
    r = client.get("/audit")
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)  # empty on the throwaway DB


def test_artist_prefers_metadata_tag():
    # the map/popup artist used to only parse "Artist - Title" names, leaving most
    # tracks blank; it now prefers the file's artist metadata tag.
    from vibedentify.routes import _artist_of

    tagged = {"tags": {"tag": {"artist": "ODESZA"}}}
    assert _artist_of(tagged, "Say My Name (feat. Zyra)", "x.mp3") == "ODESZA"
    # no tag -> fall back to 'Artist - Title' parsing
    assert _artist_of({}, "Crystal Waters - Gypsy Woman", "x.mp3") == "Crystal Waters"
    # albumartist is the secondary tag
    assert _artist_of({"tags": {"tag": {"albumartist": "V/A"}}}, "Some Title", "x.mp3") == "V/A"
    # nothing available -> empty (not a crash)
    assert _artist_of({}, "Just A Title", "x.mp3") == ""


def test_batch_analyzes_folder_and_caches(client, tmp_path):
    # a server-side folder of tiny audio files -> an NDJSON stream: a `total`
    # line, then one result per file; a re-run returns every file from cache.
    for i, name in enumerate(("a.wav", "b.wav", "c.wav")):
        (tmp_path / name).write_bytes(_tiny_wav_bytes(sample=i + 1))  # distinct content each

    def run():
        r = client.post("/batch", json={"path": str(tmp_path)})
        assert r.status_code == 200
        return [json.loads(x) for x in r.data.decode().splitlines() if x.strip()]

    lines = run()
    assert lines[0] == {"total": 3}
    results = lines[1:]
    assert len(results) == 3
    assert all(r["ok"] for r in results)
    assert all(r["cached"] is False for r in results)  # first pass: freshly analyzed
    assert all(r.get("hash") for r in results)

    again = run()  # same content hashes -> all cache hits
    assert again[0] == {"total": 3}
    assert all(r["cached"] is True for r in again[1:])


def test_batch_missing_dir_400(client):
    assert client.post("/batch", json={"path": "/no/such/dir"}).status_code == 400


def test_compare_fake_shape(client):
    # FAKE mode returns canned EffNet-vs-MAEST pairs without running a model.
    r = client.post("/compare")
    assert r.status_code == 200
    body = r.get_json()
    assert body["maest_available"] is True
    assert isinstance(body["pairs"], list) and body["pairs"]
    for p in body["pairs"]:
        for key in ("parent", "style", "eff", "mae"):
            assert key in p


def test_map_populated(client):
    # analyze a few tracks, then the map returns them as nodes; every edge only
    # ever references a real node hash.
    hashes = []
    for i, name in enumerate(("m1.wav", "m2.wav", "m3.wav")):
        data = {"file": (io.BytesIO(_tiny_wav_bytes(sample=i + 1)), name)}  # 3 distinct tracks
        h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()[
            "hash"
        ]
        hashes.append(h)
    assert len(set(hashes)) == 3  # distinct content -> distinct nodes (not deduped)

    body = client.get("/map").get_json()
    node_hashes = {n["hash"] for n in body["nodes"]}
    assert set(hashes) <= node_hashes
    for n in body["nodes"]:
        assert n["style"] and "bpm" in n
    for e in body["edges"]:
        assert e["a"] in node_hashes and e["b"] in node_hashes
        assert isinstance(e["sim"], (int, float))


def test_vibe_lifecycle(client):
    # create a vibe, add a track, weight it (Rocchio), read members, remove, read.
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "vibe.wav")}
    h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()["hash"]

    vid = client.post("/vibes", json={"name": "Peak Time"}).get_json()["id"]
    assert client.post("/vibes/add", json={"vibe_id": vid, "hash": h}).get_json()["added"] is True

    r = client.post("/vibes/weight", json={"vibe_id": vid, "hash": h, "weight": 0.5})
    assert r.get_json()["weight"] == 0.5
    # weight clamps to [-1, 1]
    clamped = client.post("/vibes/weight", json={"vibe_id": vid, "hash": h, "weight": 5})
    assert clamped.get_json()["weight"] == 1.0

    members = client.get(f"/vibes/{vid}/members").get_json()
    assert len(members) == 1
    assert members[0]["hash"] == h and members[0]["weight"] == 1.0

    assert (
        client.post("/vibes/remove", json={"vibe_id": vid, "hash": h}).get_json()["removed"] is True
    )
    assert client.get(f"/vibes/{vid}/members").get_json() == []


def test_similar_returns_neighbors(client):
    hashes = []
    for name in ("s1.wav", "s2.wav", "s3.wav"):
        data = {"file": (io.BytesIO(_tiny_wav_bytes()), name)}
        hashes.append(
            client.post("/analyze", data=data, content_type="multipart/form-data").get_json()[
                "hash"
            ]
        )
    body = client.get(f"/similar/{hashes[0]}?k=5").get_json()
    assert isinstance(body, list)
    for row in body:
        assert row["hash"] in set(hashes) and row["hash"] != hashes[0]  # excludes self
        assert "sim" in row


def test_vibe_match_and_playlist(client):
    data = {"file": (io.BytesIO(_tiny_wav_bytes()), "vm.wav")}
    h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()["hash"]
    vid = client.post("/vibes", json={"name": "V"}).get_json()["id"]
    client.post("/vibes/add", json={"vibe_id": vid, "hash": h})
    # match: the track scored against every vibe's centroid
    m = client.get(f"/vibes/match/{h}").get_json()
    assert any(x["id"] == vid and "sim" in x for x in m)
    # playlist: whole-DB ranking vs the vibe centroid (the lone member scores ~1.0)
    pl = client.get(f"/vibes/{vid}/playlist").get_json()
    assert isinstance(pl, list) and any(row["hash"] == h for row in pl)


def test_vibe_routes_require_fields(client):
    assert client.post("/vibes/add", json={}).status_code == 400
    assert client.post("/vibes/weight", json={"vibe_id": 1}).status_code == 400
    assert client.post("/vibes/remove", json={}).status_code == 400


def test_second_style_override_returns_none():
    # an override wins outright -> no 2nd genre to blend toward, even if the raw
    # salience still lists a runner-up.
    from vibedentify.routes import _second_style

    payload = {"override": "Riddim", "salience": [{"style": "House", "score": 0.4}]}
    assert _second_style(payload, "Dubstep", 0.6) is None


def test_second_style_runner_up_weight():
    # weight2 = sc / (top_score + sc), rounded to 3 places, <= 0.5 when the caller
    # passes a genuine top score (top_score >= runner-up score).
    from vibedentify.routes import _second_style

    payload = {"salience": [{"style": "Techno", "score": 0.5}, {"style": "House", "score": 0.3}]}
    assert _second_style(payload, "Techno", 0.5) == ["House", 0.375]  # 0.3 / 0.8

    # rounds to 3 places: 0.3 / 0.85 = 0.352941... -> 0.353
    payload = {"salience": [{"style": "Techno", "score": 0.55}, {"style": "House", "score": 0.3}]}
    assert _second_style(payload, "Techno", 0.55) == ["House", 0.353]

    # a runner-up as strong as the top pins the weight at its 0.5 ceiling
    payload = {"salience": [{"style": "Techno", "score": 0.4}, {"style": "House", "score": 0.4}]}
    assert _second_style(payload, "Techno", 0.4) == ["House", 0.5]


def test_second_style_no_distinct_runner_up():
    # nothing but the top style (or duplicates of it) -> no runner-up -> None.
    from vibedentify.routes import _second_style

    only_top = {"salience": [{"style": "Techno", "score": 0.5}]}
    assert _second_style(only_top, "Techno", 0.5) is None

    dupes = {"salience": [{"style": "Techno", "score": 0.5}, {"style": "Techno", "score": 0.3}]}
    assert _second_style(dupes, "Techno", 0.5) is None


def test_second_style_falls_back_to_styles():
    # salience is preferred, but a missing/empty salience falls through to styles;
    # with neither, there's no runner-up.
    from vibedentify.routes import _second_style

    styles = [{"style": "Techno", "score": 0.5}, {"style": "House", "score": 0.3}]
    assert _second_style({"styles": styles}, "Techno", 0.5) == ["House", 0.375]  # salience absent
    assert _second_style({"salience": [], "styles": styles}, "Techno", 0.5) == ["House", 0.375]
    assert _second_style({"salience": [], "styles": []}, "Techno", 0.5) is None  # both empty
    assert _second_style({}, "Techno", 0.5) is None  # neither key present


def test_second_style_zero_top_score_no_zero_division():
    # a zero/None top_score must not raise: denom = (top_score or 0) + sc is always
    # >= sc > 0 here, so the division is safe. With a zero top the weight comes out
    # at 1.0 -- outside the docstring's [0, 0.5], but the map clamps the mix weight
    # to 0.5 when rendering, so this degenerate input is harmless downstream.
    from vibedentify.routes import _second_style

    payload = {"salience": [{"style": "Techno", "score": 0.0}, {"style": "House", "score": 0.3}]}
    assert _second_style(payload, "Techno", 0) == ["House", 1.0]
    assert _second_style(payload, "Techno", None) == ["House", 1.0]


def _analyze_tracks(client, names):
    # analyze a set of distinct-content tracks; return their hashes in order.
    hashes = []
    for i, name in enumerate(names):
        data = {"file": (io.BytesIO(_tiny_wav_bytes(sample=i + 1)), name)}
        h = client.post("/analyze", data=data, content_type="multipart/form-data").get_json()[
            "hash"
        ]
        hashes.append(h)
    return hashes


def test_training_candidates_empty_centroid(client):
    # tracks exist but none are labelled the genre -> no centroid -> a clear
    # message (not an error), and an empty candidate list.
    _analyze_tracks(client, ("t1.wav", "t2.wav"))
    r = client.get("/training/candidates/Riddim")
    assert r.status_code == 200
    body = r.get_json()
    assert body["labeled"] == 0
    assert body["candidates"] == []
    assert body.get("message")  # non-empty guidance string


def test_training_candidates_rank_and_exclusions(client):
    hashes = _analyze_tracks(client, ("a.wav", "b.wav", "c.wav", "d.wav"))
    # seed the genre centroid by overriding one track to it
    seed = hashes[0]
    assert client.post(f"/override/{seed}", json={"genre": "Riddim"}).status_code == 200

    body = client.get("/training/candidates/Riddim").get_json()
    assert body["labeled"] >= 1
    cands = body["candidates"]
    assert cands, "the remaining tracks should be ranked as candidates"
    ch = [c["hash"] for c in cands]
    assert seed not in ch  # the override-labelled track is excluded

    # ordering exists and is by descending similarity
    sims = [c["sim"] for c in cands]
    assert sims == sorted(sims, reverse=True)
    # every candidate carries the promised shape
    for c in cands:
        for key in ("hash", "title", "sim", "bpm", "camelot"):
            assert key in c

    # reject one -> it never resurfaces for this genre
    victim = ch[0]
    assert (
        client.post("/training/reject", json={"hash": victim, "genre": "Riddim"}).status_code == 200
    )
    ch2 = [c["hash"] for c in client.get("/training/candidates/Riddim").get_json()["candidates"]]
    assert victim not in ch2

    # confirm another -> recorded as a label, so it drops out of the queue too
    keep = ch2[0]
    r = client.post("/training/confirm", json={"hash": keep, "genre": "Riddim"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    ch3 = [c["hash"] for c in client.get("/training/candidates/Riddim").get_json()["candidates"]]
    assert keep not in ch3


def test_training_confirm_reject_validation(client):
    # both routes require hash + genre; confirm 404s on an unknown track.
    assert client.post("/training/confirm", json={}).status_code == 400
    assert client.post("/training/reject", json={}).status_code == 400
    assert (
        client.post("/training/confirm", json={"hash": "nope", "genre": "Riddim"}).status_code
        == 404
    )


def test_training_confirm_clears_prior_reject(client):
    # a confirm on a previously-rejected track wins: it becomes a label and the
    # stale reject is cleared (so it's excluded as a label, not resurrected).
    (h,) = _analyze_tracks(client, ("solo.wav",))
    assert client.post("/training/reject", json={"hash": h, "genre": "Riddim"}).status_code == 200
    r = client.post("/training/confirm", json={"hash": h, "genre": "Riddim"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # now labelled: it seeds the centroid and is not offered as a candidate
    body = client.get("/training/candidates/Riddim").get_json()
    assert body["labeled"] >= 1
    assert h not in [c["hash"] for c in body["candidates"]]


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
