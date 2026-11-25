"""Stable local music references in Parole's existing library index."""
import json
from pathlib import Path
from stat import S_ISREG

from _parole_index import document, encoded_key
from _parole_fingerprint import parse as parse_fingerprint, similarity
from _parole_music_identity import (CONFIRMED, DIFFERENT, Track, barcode, duration_ms, isrc,
                                    mbid, normalized, position, same_track, year)

# Keep the original source values relevant to comparison. Lyrics and
# enrichment are owned by the library and never enter the metadata snapshot.
IDENTITY_TAGS = {
    "title", "artist", "albumartist", "album", "disc", "discnumber", "track", "tracknumber",
    "year", "date", "edition", "albumdisambiguation", "albumid", "artistid", "recordingid",
    "musicbrainzartistid", "musicbrainzalbumartistid", "musicbrainzalbumid",
    "musicbrainzreleasegroupid", "musicbrainzrecordingid", "musicbrainztrackid", "musicbrainzworkid",
    "isrc", "barcode", "upc", "ean", "duration_seconds", "codec", "bitrate",
}


def original_tags(tags):
    return {name: value if type(value) in (float, int) else value[:1000]
            for name, value in tags.items()
            if name in IDENTITY_TAGS and type(value) in (str, float, int)}


def edition_metadata(tags):
    return {
        "title": normalized(tags.get("album")), "artist": normalized(tags.get("albumartist") or tags.get("artist")),
        "year": year(tags.get("date") or tags.get("year")),
        "mb_artist": mbid(tags.get("musicbrainzalbumartistid") or tags.get("artistid") or tags.get("musicbrainzartistid")),
        "mb_group": mbid(tags.get("musicbrainzreleasegroupid")),
        "mb_release": mbid(tags.get("musicbrainzalbumid") or tags.get("albumid")),
        "gtin": barcode(tags.get("barcode") or tags.get("ean") or tags.get("upc")),
    }


def recording_metadata(tags, pcm):
    return {
        "title": normalized(tags.get("title")), "artist": normalized(tags.get("artist") or tags.get("albumartist")),
        "pcm": pcm,
        "mb_recording": mbid(tags.get("musicbrainzrecordingid") or tags.get("musicbrainztrackid") or tags.get("recordingid")),
        "mb_work": mbid(tags.get("musicbrainzworkid")), "isrc": isrc(tags.get("isrc")),
    }


def comparison_tags(proof):
    return {**proof.get("catalogue_tags", {}), **proof.get("tags", {})}


def set_alias(index, album, first, second, *, enabled):
    title = normalized(album)
    names = sorted((normalized(value), value.strip()) for value in (first, second))
    if (not title or not all(key for key, _ in names) or names[0][0] == names[1][0]
            or any(len(value) > 180 for value in (album, first, second))):
        raise ValueError("Indique deux graphies distinctes et leur album commun.")
    (left, first_name), (right, second_name) = names
    with index.db() as db:
        if enabled:
            db.execute("INSERT OR IGNORE INTO canonical_aliases VALUES(?,?,?,?,?)",
                       (title, left, right, first_name, second_name))
        else:
            db.execute("DELETE FROM canonical_aliases WHERE album=? AND first_key=? AND second_key=?", (title, left, right))


def approved_aliases(index, album):
    if index is None:
        return ()
    with index.db() as db:
        # Legacy artist regroupements already approved for the same album
        # remain evidence. New aliases are comparison-only and never move it.
        return tuple(tuple(row) for row in db.execute("""
            SELECT first_name,second_name FROM canonical_aliases WHERE album=?
            UNION SELECT old_artist,new_artist FROM associations WHERE album=? ORDER BY 1,2 LIMIT 100
        """, (normalized(album), album)))


def track_metadata(tags, pcm=""):
    metadata = recording_metadata(tags, pcm)
    title, artist = tags.get("title") or "", tags.get("artist") or tags.get("albumartist") or ""
    return Track(position(tags.get("disc") or tags.get("discnumber")) or 1,
                 position(tags.get("track") or tags.get("tracknumber")), title, duration_ms(tags.get("duration_seconds")),
                 metadata["mb_recording"], metadata["mb_work"], metadata["isrc"], metadata["pcm"], artist)


def recognized_path(root, value):
    if not isinstance(value, str):
        return None
    target = Path(value)
    if (not target.is_absolute() or not target.is_relative_to(root) or len(target.relative_to(root).parts) < 3
            or target.relative_to(root).parts[0] in {"A_trier", "YouTube"}):
        return None
    try:
        if target.resolve(strict=True) == target and S_ISREG(target.lstat().st_mode):
            return target
    except (OSError, RuntimeError):
        pass
    return None


def store_import(index, proofs, *, fingerprints=None):
    if not proofs:
        return
    # Metadata, edition references, provenance and recordings become visible
    # together. No foreign file is modified by the identity registry.
    with index.db() as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for proof in proofs:
            target = recognized_path(index.root, proof.get("path"))
            if not target:
                continue
            tags = comparison_tags(proof)
            pcm = proof.get("pcm", "")
            recording = recording_metadata(tags, pcm)
            edition = edition_metadata(tags)
            track = track_metadata(tags, pcm)
            identity_origin = pcm or proof.get("sha256") or (proof["provider"], proof["external_id"])
            recording_key = encoded_key("recording-v1", identity_origin, recording["title"], recording["artist"])
            # Evidence for a shared release must not collapse performances.
            # Editions with the same folder spelling may still have distinct IDs.
            edition_key = encoded_key("edition-v1", str(target.parent.relative_to(index.root)), edition["title"],
                                     edition["artist"], edition["year"], edition["mb_release"], edition["gtin"])
            reuse = proof.get("canonical_reference")
            if reuse:
                prior = db.execute("SELECT edition,recording FROM canonical_references WHERE id=? AND path=?",
                                   (reuse, str(target))).fetchone()
                if prior:
                    edition_key, recording_key = prior
            db.execute("INSERT OR IGNORE INTO canonical_editions VALUES(?,?,?,?,?,?,?,?)",
                       (edition_key, edition["title"], edition["artist"], edition["year"], edition["mb_artist"],
                        edition["mb_group"], edition["mb_release"], edition["gtin"]))
            db.execute("INSERT OR IGNORE INTO canonical_recordings VALUES(?,?,?,?,?,?,?)",
                       (recording_key, recording["title"], recording["artist"], pcm,
                        recording["mb_recording"], recording["mb_work"], recording["isrc"]))
            kind, external = proof["provider"], proof["external_id"]
            reference = encoded_key("provider-reference-v1", kind, external, proof.get("edition_ref"),
                                   proof.get("source"), proof.get("member"), proof.get("sha256") or pcm)
            song = db.execute("SELECT id FROM songs WHERE path=?", (str(target),)).fetchone()
            evidence = {key: proof[key] for key in ("source", "member", "license", "verification", "edition_ref",
                                                   "position", "canonical_reference", "catalogue_tags", "acoustic")
                        if key in proof}
            db.execute("""
                INSERT INTO canonical_references VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path, song=CASE WHEN excluded.song!='' THEN excluded.song ELSE canonical_references.song END,
                    edition=excluded.edition, recording=excluded.recording, sha256=excluded.sha256,
                    duration=excluded.duration, tags=excluded.tags, evidence=excluded.evidence
            """, (reference, kind, external, edition_key, recording_key, str(target), song[0] if song else "",
                  proof.get("sha256", ""), track.disc, track.number, track.millis, CONFIRMED,
                  document(proof.get("tags", {})), document(evidence)))
            if fingerprints and (fingerprint := fingerprints.get(pcm)):
                db.execute("INSERT OR IGNORE INTO canonical_fingerprints VALUES(?,?)", (pcm, fingerprint))


def stored_fingerprint(index, pcm):
    with index.db(wait=.35) as db:
        row = db.execute("SELECT fingerprint FROM canonical_fingerprints WHERE pcm=?", (pcm,)).fetchone()
    return row[0] if row else ""


def acoustic_evidence(index, proof):
    current = parse_fingerprint(json.loads(proof["fingerprint"]))
    if current is None:
        return []
    tags = comparison_tags(proof)
    recording = recording_metadata(tags, proof["pcm"])
    local_track = track_metadata(tags)
    with index.db(wait=.35) as db:
        rows = db.execute("""
            SELECT p.id,p.path,p.tags,p.evidence,r.pcm,f.fingerprint
            FROM canonical_recordings r JOIN canonical_fingerprints f ON f.pcm=r.pcm
            JOIN canonical_references p ON p.recording=r.id
            WHERE r.title=? AND r.artist=? AND r.pcm!=? ORDER BY p.path,p.id LIMIT 12
        """, (recording["title"], recording["artist"], recording["pcm"])).fetchall()
    results = []
    reviewed = set()
    for row in rows:
        if row["pcm"] in reviewed or not recognized_path(index.root, row["path"]):
            continue
        reviewed.add(row["pcm"])
        other = parse_fingerprint(json.loads(row["fingerprint"]))
        other_tags = comparison_tags({"tags": json.loads(row["tags"]),
                                      "catalogue_tags": json.loads(row["evidence"]).get("catalogue_tags", {})})
        relation = same_track(local_track, track_metadata(other_tags))
        if relation == DIFFERENT or other is None:
            continue
        score = similarity(current, other)
        if score is not None and score >= .93:
            # Even a strong prefix is complementary evidence. No file or
            # timing cache is reused through this comparison.
            results.append({"reference": row["id"], "confiance": relation, "similarite": round(score, 4),
                            "durees_ms": [current.millis, other.millis]})
    return results


def possible_duplicates(index, proof):
    tags = comparison_tags(proof)
    source = proof["sha256"]
    pcm = proof["pcm"]
    edition = edition_metadata(tags)
    current_track = track_metadata(tags, pcm)
    with index.db(wait=.35) as db:
        rows = db.execute("""
            SELECT p.id,p.path,p.sha256,p.disc,p.track,p.tags,p.evidence,r.pcm,e.mb_release
            FROM canonical_references p JOIN canonical_recordings r ON r.id=p.recording
            JOIN canonical_editions e ON e.id=p.edition
            WHERE p.sha256=? OR (r.pcm=? AND e.mb_release!='' AND e.mb_release=?)
            ORDER BY p.sha256=? DESC,p.path,p.id LIMIT 24
        """, (source, pcm, edition["mb_release"], source)).fetchall()
        existing = db.execute("""
            SELECT path,info,disc,track,duration,release_id FROM songs
            WHERE (:release!='' AND release_id=:release AND track=:track)
               OR (owner=:owner AND album=:album AND track IN (0,:track))
            ORDER BY path LIMIT 24
        """, {"release": edition["mb_release"], "track": current_track.number,
              "owner": tags.get("albumartist") or tags.get("artist", ""), "album": tags.get("album", "")}).fetchall()
    for row in rows:
        target = recognized_path(index.root, row["path"])
        if not target:
            continue
        if edition["mb_release"] and row["mb_release"] and row["mb_release"] != edition["mb_release"]:
            continue
        if row["sha256"] == source:
            yield row["id"], target, True
            continue
        other_tags = comparison_tags({"tags": json.loads(row["tags"]),
                                      "catalogue_tags": json.loads(row["evidence"]).get("catalogue_tags", {})})
        other = edition_metadata(other_tags)
        if (edition["title"] != other["title"] or edition["artist"] != other["artist"]
                or not current_track.number or (row["disc"], row["track"]) != (current_track.disc, current_track.number)
                or normalized(tags.get("edition")) != normalized(other_tags.get("edition"))):
            continue
        if same_track(current_track, track_metadata(other_tags, row["pcm"])) == CONFIRMED:
            yield row["id"], target, True
    for row in existing:
        target = recognized_path(index.root, row["path"])
        if not target or edition["mb_release"] and row["release_id"] and edition["mb_release"] != row["release_id"]:
            continue
        metadata = json.loads(row["info"]).get("tags", {})
        if normalized(metadata.get("title")) != normalized(tags.get("title")):
            continue
        other = track_metadata({**metadata, "duration_seconds": row["duration"]})
        album = edition_metadata(metadata)
        # The inventory alone is only a lead. An established release and
        # recording permit testing PCM. A weaker lead can test exact bytes.
        allow_pcm = (bool(edition["mb_release"] and edition["mb_release"] == row["release_id"])
                     and edition["title"] == album["title"] and edition["artist"] == album["artist"]
                     and (current_track.disc, current_track.number) == (other.disc, other.number)
                     and same_track(current_track, other) == CONFIRMED)
        yield "", target, allow_pcm
