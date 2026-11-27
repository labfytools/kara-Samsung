#!/usr/bin/python3 -B
"""Qualification hors réseau des admissions et de la provenance V3."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from time import sleep, time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from _kara_prepare import file_token
from _parole_index import Index
from _parole_lyrics_lookup import CACHE_KIND, EVIDENCE_KIND, TEMPORARY_KIND, playback_candidates
from _parole_lrclib_local import candidates
from _parole_sources import (configure_limits, lyrics_rows, move_lyrics_source, ready_sources,
                             record_quota, seed)
from _parole_work import lyrics_engine

SONG = ("Chanson", "Groupe", "Album", 30, "Groupe")
TIMED = "[00:01.00]Premier mot"
WORD = """version: '1.0'
metadata: {}
lines:
- text: 'Premier mot'
  start_ms: 1000
  end_ms: 2400
  words:
  - text: 'Premier '
    start_ms: 1000
  - text: 'mot'
    start_ms: 1804
- text: 'Ligne sans détails'
  start_ms: 4500
"""


class LyricsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="parole-lyrics-v3-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.library = self.base / "music"
        self.library.mkdir()
        self.index = Index(self.library, self.base / "state")
        seed(self.index)
        self.engine = lyrics_engine()

    def only(self, *providers):
        with self.index.db() as db:
            db.execute("UPDATE sources SET state='disabled'")
            db.executemany("UPDATE sources SET state='active' WHERE id=?", [(name,) for name in providers])

    def test_two_independent_429_fallback_and_no_false_negative(self):
        self.only("LRCLIB", "NetEase", "QQ Music", "lyrics.ovh")
        client = self.engine.LyricsSources()

        def denied(request, **kwargs):
            url = request.full_url
            raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "120"}, None)
        # All HTTP admissions are real SQLite reservations. Only the network,
        # the shared production rate files, and QQ's response are substituted.
        with patch("_parole_sources.wait_service"), patch("_parole_sources.resource_slot",
                return_value=nullcontext()), patch("_parole_sources.defer_service"), patch.object(
                self.engine, "resource_slot", side_effect=lambda *a, **kw: nullcontext()), patch.object(
                self.engine, "defer_service"), patch.object(client.lrc.opener, "open", side_effect=denied), patch.object(
                client.netease.opener, "open", side_effect=denied), patch.object(
                client.qqmusic, "find", return_value="Texte trouvé") as qq, patch.object(
                client.ovh, "find", side_effect=AssertionError("agrégateur interdit pendant la pause")):
            self.assertEqual(client.find(*SONG, synced=False, registry=self.index, strict=True),
                             ("Texte trouvé", "QQ Music"))
            self.assertEqual(client.find(*SONG, synced=False, registry=self.index, strict=True),
                             ("Texte trouvé", "QQ Music"))
            self.assertEqual(qq.call_count, 1)
            self.assertEqual(client.cache_hits, 1)
            with qq:
                qq.return_value = None
                with self.assertRaises(self.engine.ServiceUnavailable):
                    client.find("Autre titre", *SONG[1:], synced=False, registry=self.index, strict=True)
        rows = {row["id"]: row for row in lyrics_rows(self.index)}
        for name in ("LRCLIB", "NetEase"):
            self.assertEqual(rows[name]["used_today"], 1)
            self.assertEqual(rows[name]["last_http"], 429)
            self.assertGreater(rows[name]["next_available"], time() + 114)
        with self.index.db() as db:
            values = db.execute("SELECT state,value FROM reviews WHERE kind=?", (CACHE_KIND,)).fetchall()
            delays = db.execute("SELECT value FROM reviews WHERE kind=?", (TEMPORARY_KIND,)).fetchall()
        self.assertEqual([row["state"] for row in values].count("absent"), 1)
        self.assertEqual([row["state"] for row in values].count("verified"), 1)
        self.assertEqual({json.loads(row[0])["provider"] for row in delays}, {"LRCLIB", "NetEase"})

    def test_concurrent_lookup_reuses_result_when_priority_and_budget_change(self):
        self.only("LRCLIB")
        gate = Barrier(2)
        opts = dict(synced=True, registry=self.index, strict=True, isrc="FRABC1900001")
        clients = [self.engine.LyricsSources() for _ in range(2)]

        def lookup(client):
            gate.wait(timeout=2)
            return client.find(*SONG, **opts)

        def remote(*args, **kwargs):
            sleep(.15)
            return TIMED
        with patch.object(self.engine.LrcLib, "find", side_effect=remote) as query:
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lookup, clients))
            self.assertEqual(results, [(TIMED, "LRCLIB")] * 2)
            query.assert_called_once()
            configure_limits(self.index, "LRCLIB", 0, 12)
            move_lyrics_source(self.index, "LRCLIB", 1)
            self.assertNotIn("LRCLIB", ready_sources(self.index, time()))
            self.assertEqual(clients[0].find(*SONG, **opts), (TIMED, "LRCLIB"))
            query.assert_called_once()
            self.assertEqual(lyrics_rows(self.index)[0]["id"], "NetEase")
            with self.assertRaises(self.engine.ServiceUnavailable):
                clients[0].find(*SONG, **dict(opts, isrc="FRABC1900002"))

    def test_simultaneous_text_and_lrc_share_exact_lrclib_response(self):
        self.only("LRCLIB")
        gate = Barrier(2)
        record = dict(id=800, trackName=SONG[0], artistName=SONG[1], albumName=SONG[2], duration=30,
                      plainLyrics="Premier mot", syncedLyrics=TIMED)

        def lookup(synced):
            client = self.engine.LyricsSources()
            gate.wait(timeout=2)
            return client.find(*SONG, synced=synced, registry=self.index, strict=True)

        with patch.object(self.engine.LrcLib, "get", return_value=record) as query:
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lookup, (True, False)))
            query.assert_called_once()
        self.assertEqual(results, [(TIMED, "LRCLIB"), ("Premier mot", "LRCLIB")])

    def test_word_timing_fallback_isrc_and_alternate_evidence(self):
        from _parole_lyricsfile import enhanced

        converted = enhanced(WORD, 30)
        self.assertIn("[00:01.000]<00:01.000>Premier <00:01.804>mot", converted)
        self.assertIn("[00:04.500]Ligne sans détails", converted)
        self.assertIsNone(enhanced(WORD.replace("1804", "900"), 30))
        self.assertIsNone(enhanced(WORD.replace("metadata: {}", "metadata: {}\nmetadata: {}"), 30))
        self.assertIsNone(enhanced(WORD.replace("metadata: {}", "metadata: &anchor {}"), 30))
        self.assertIsNone(enhanced(WORD.replace("text: 'mot'", "text: 'intrus'"), 30))
        self.only("LRCLIB", "QQ Music")
        move_lyrics_source(self.index, "QQ Music", -1)
        move_lyrics_source(self.index, "QQ Music", -1)
        path = self.library / "song.flac"
        path.write_bytes(b"first recording")
        client = self.engine.LyricsSources()
        record = dict(id=800, trackName=SONG[0], artistName=SONG[1], albumName=SONG[2], duration=30,
                      isrc="FRABC1900001", lyricsfile=WORD, hasWordSync=True)
        alternate = dict(record, id=801, isrc="FRABC1900002", lyricsfile=None,
                         hasWordSync=False, syncedLyrics=TIMED)
        self.assertIsNone(self.engine.verified_lyrics(alternate, *SONG, synced=True, isrc=record["isrc"]))
        with patch.object(client.qqmusic, "find", return_value=TIMED), patch.object(
                client.lrc, "get", side_effect=[alternate, [record]]):
            self.assertEqual(client.find(*SONG, synced=True, registry=self.index, strict=True,
                                         isrc=record["isrc"], recording=(path, file_token(path))),
                             (converted, "LRCLIB"))
        with self.index.db() as db:
            evidence = json.loads(db.execute("SELECT value FROM reviews WHERE kind=?",
                                             (EVIDENCE_KIND,)).fetchone()[0])["synced"]
        self.assertIn(("LRCLIB", "other-edition", 801),
                      [(r["provider"], r["decision"], r["metadata"].get("catalogue_id")) for r in evidence])
        self.assertIn(("LRCLIB", "selected", 800),
                      [(r["provider"], r["decision"], r["metadata"].get("catalogue_id")) for r in evidence])
        with patch("_kara_paths.STATE", self.base / "state"):
            snapshot = playback_candidates(path, self.library)
            self.assertTrue(snapshot["synced"])
            self.assertTrue(all("text" not in result for result in snapshot["synced"]))
            path.write_bytes(b"another recording on the same path")
            self.assertEqual(playback_candidates(path, self.library), {})

    def test_progressive_discovery_keeps_original_credits_and_strict_timing(self):
        engine = self.engine
        title, artist, album = "03 - L’Été (feat․ Invité)", "Artiste, Invité", "Original"
        args = (title, artist, album, 100, "Artiste")
        record = dict(trackName="L’Été (feat. Invité)", artistName="Artiste",
                      albumName="Compilation", duration=104, plainLyrics="Texte de l’enregistrement",
                      syncedLyrics="[00:01.00]Texte de l’enregistrement")
        calls = []

        def get(endpoint, **terms):
            calls.append((endpoint, terms))
            return [record] if terms.get("artist_name") == "Invité" else None

        client = engine.LrcLib()
        with patch.object(client, "get", side_effect=get):
            self.assertEqual(client.find(*args), record["plainLyrics"])
            self.assertIsNone(client.find(*args, synced=True))
        self.assertEqual(calls[0][1]["track_name"], title)
        self.assertEqual(calls[0][1]["album_name"], album)
        self.assertIn(("search", {"track_name": "l ete", "artist_name": "artiste"}), calls)
        self.assertIn(("search", {"track_name": "L’Été", "artist_name": "Invité"}), calls)
        self.assertIsNone(engine.verified_lyrics(dict(record, trackName="L’Été (Live)"), *args))

    def test_ambiguity_requires_corroboration_and_preserves_candidates(self):
        self.only("LRCLIB", "NetEase", "QQ Music")
        client = self.engine.LyricsSources()
        recording = self.library / "song.flac"
        recording.write_bytes(b"fixture")
        record = dict(id=1, trackName=SONG[0], artistName=SONG[1], albumName=SONG[2], duration=30,
                      plainLyrics="Premier texte", syncedLyrics="[00:01.00]Premier texte")
        other = dict(record, id=2, plainLyrics="Autre texte", syncedLyrics="[00:02.00]Premier texte")
        events = []
        with patch.object(client.lrc, "get", side_effect=[None, [record, other]]), patch.object(
                client.netease, "find", return_value="Premièr texte !"), patch.object(
                client.qqmusic, "find", return_value=None) as later:
            self.assertEqual(client.find(*SONG, synced=False, strict=True, registry=self.index,
                                         recording=(recording, file_token(recording)), audit=events.append),
                             ("Premier texte", "LRCLIB"))
        later.assert_called()
        self.assertIn(("LRCLIB", "ambiguous"), [(e.get("provider"), e.get("outcome")) for e in events])
        with patch.object(client.lrc, "get", side_effect=[None, [record, other]]), patch.object(
                client.netease, "find", return_value=None), patch.object(client.qqmusic, "find", return_value=None):
            with self.assertRaises(self.engine.AmbiguousLyrics):
                client.find(*SONG, synced=True, strict=True, registry=self.index, local_text="Premier texte",
                            recording=(recording, file_token(recording)))
        with self.index.db() as db:
            row = db.execute("SELECT value FROM reviews WHERE kind=?", (EVIDENCE_KIND,)).fetchone()
        self.assertEqual({v["metadata"]["catalogue_id"] for v in json.loads(row[0])["synced"]}, {1, 2})

    def test_artist_outcomes_are_exclusive_and_provider_yield_counts_publications(self):
        from _parole_import_enrichment import ARTIST_OUTCOMES, artist_summary

        cases = (("present", "present"), ("found", "limited"), ("found", "found"), ("present", "review"),
                 ("missing", "missing"), ("missing", "limited"), ("temporary", "missing"))
        audit = [{"type": "request", "provider": "LRCLIB", "sent": True},
                 {"type": "provider", "provider": "LRCLIB", "outcome": "found"},
                 {"type": "publication", "provider": "LRCLIB", "form": "lrc"}]
        rows = {i: {"lyrics": plain, "lrc": timed} for i, (plain, timed) in enumerate(cases)}
        rows[2]["audit"] = audit
        self.assertEqual(artist_summary(rows), (dict.fromkeys(ARTIST_OUTCOMES, 1),
                         {"LRCLIB": {"requests": 1, "found": 1, "published_lrc": 1}}))

    def test_quota_dashboard_keeps_local_and_remote_limits_distinct(self):
        configure_limits(self.index, "NetEase", 2, 1)
        self.assertTrue(self.index.reserve_daily("provider:NetEase", 2))
        record_quota(self.index, "NetEase", {"RateLimit-Remaining": "7"})
        row = next(row for row in lyrics_rows(self.index) if row["id"] == "NetEase")
        self.assertEqual((row["used_today"], row["remaining"], row["remote_quota"]), (1, 1, 7))
        self.assertTrue(self.index.reserve_daily("provider:NetEase", 2))
        row = next(row for row in lyrics_rows(self.index) if row["id"] == "NetEase")
        self.assertEqual(row["availability"], "limited")
        self.assertGreater(row["next_available"], time())
        row = next(row for row in lyrics_rows(self.index, now=time() + 301) if row["id"] == "NetEase")
        self.assertIsNone(row["remote_quota"])

    def test_offline_snapshot_uses_record_metadata_in_read_only_mode(self):
        database = self.base / "snapshot.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE tracks (id INTEGER PRIMARY KEY, name TEXT, name_lower TEXT,
                  artist_name TEXT, artist_name_lower TEXT, album_name TEXT, album_name_lower TEXT,
                  duration REAL, last_lyrics_id INTEGER, UNIQUE(name_lower, artist_name_lower, album_name_lower, duration));
                CREATE TABLE lyrics (id INTEGER PRIMARY KEY, instrumental INTEGER, has_word_sync INTEGER,
                  plain_lyrics TEXT, synced_lyrics TEXT, lyricsfile TEXT);
                INSERT INTO tracks VALUES (800,'Chanson','chanson','Groupe','groupe','Album','album',30,810);
                INSERT INTO tracks VALUES (2,'Chanson','chanson','Groupe','groupe','Edition live','edition live',42,801);
                INSERT INTO lyrics VALUES (801,0,0,'Autre prise',NULL,NULL);
            """)
            db.execute("INSERT INTO lyrics VALUES (810,0,1,'Premier mot',?,?)", (TIMED, WORD))
        before = database.read_bytes()
        self.assertEqual([row["id"] for row in candidates(database, *SONG)], [800])
        self.assertEqual(candidates(database, "Chanson", "Groupe", "Edition live", 30, "Groupe"), [])
        with patch.dict(os.environ, {"PAROLE_LRCLIB_SQLITE": str(database)}):
            value, source = self.engine.LyricsSources().find(*SONG, synced=True, registry=self.index)
        self.assertEqual(source, "LRCLIB local")
        self.assertIn("<00:01.804>", value)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(list(self.base.glob("snapshot.sqlite3-*")), [])


if __name__ == "__main__":
    unittest.main()
