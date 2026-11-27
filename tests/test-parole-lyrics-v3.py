#!/usr/bin/python3 -B
"""Qualification hors réseau des admissions et de la provenance V3."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from time import sleep, time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from _kara_files import file_token
from _parole_index import Index, document
from _parole_lrc_credit import RED, UNISON, load_links
from _parole_lyrics_lookup import CACHE_KIND, EVIDENCE_KIND, TEMPORARY_KIND, playback_candidates, recording_candidates
from _parole_lrclib_local import candidates
from _parole_sources import (KUGOU, configure_limits, lyrics_rows, move_lyrics_source, night_lrc_providers, ready_sources,
                             record_quota, seed)
from _parole_work import import_lrc_candidate, lyrics_engine

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

    def test_native_lrc_preserves_original_payload_and_existing_file(self):
        self.only("LRCLIB")
        original = "\ufeff[00:01.00]<00:01.00>Mot  \r\n[00:02.25]Suite"
        client = self.engine.LyricsSources()
        with patch.object(client.lrc, "find", return_value=original):
            found = client.find(*SONG, synced=True, original_lrc=True, registry=self.index)
        self.assertEqual(found, (original, "LRCLIB"))
        destination = self.library / "track.lrc"
        self.engine.save_lrc(destination, original, original=True)
        self.engine.save_lrc(destination, TIMED, original=True)
        self.assertEqual(destination.read_bytes(), original.encode("utf-8"))

    def test_lrc_red_verifies_edition_and_preserves_bytes_and_attribution_from_cache(self):
        self.only(RED)
        raw = "\ufeff[00:01.00]<00:01.00>Original  \r\n[00:02.50]Suite\r\n"
        code = "GBUM71029604"
        record = dict(isrc=code, title=SONG[0], artist=SONG[1], album=SONG[2], duration=SONG[3])
        album_search = {"hits": [dict(record, isrc="GBUM71029605", album="Autre édition"), record]}
        metadata = dict(record, files={"lrc": "https://not-the-catalogue.test/file.lrc"})
        client = self.engine.LyricsSources()
        options = dict(synced=True, original_lrc=True, strict=True, registry=self.index)
        with patch.dict(os.environ, KARA_LRC_RED_CONTACT=""):
            self.assertNotIn(RED, ready_sources(self.index, time()))
            self.assertEqual(client.find(*SONG, **options), (None, None))
        with patch.dict(os.environ, KARA_LRC_RED_CONTACT="public@example.org"), patch(
            "_parole_lrc_catalogues.fetch", side_effect=[album_search, metadata, raw.encode()]) as request:
            self.assertEqual(client.find(*SONG, **options), (raw, RED))
            self.assertEqual([call.args[0] for call in request.call_args_list], [
                "https://lrc.red/search.json?q=Chanson+Groupe", f"https://lrc.red/s/{code}.json",
                f"https://lrc.red/s/{code}.lrc"])
            self.assertIn("public@example.org", request.call_args.kwargs["user_agent"])
        digest = sha256(raw.encode()).hexdigest()
        self.assertEqual(load_links(self.index, digest), [
            {"url": f"https://lrc.red/s/{code}", "label": "Paroles de lrc.red"}])
        configure_limits(self.index, RED, 0, 0)
        with patch.dict(os.environ, KARA_LRC_RED_CONTACT="public@example.org"), patch(
            "_parole_lrc_catalogues.fetch", side_effect=AssertionError("le cache garde le LRC attribué")):
            self.assertEqual(self.engine.LyricsSources().find(*SONG, **options), (raw, RED))
        destination = self.library / "track.lrc"
        self.engine.save_lrc(destination, raw, original=True)
        self.engine.save_lrc(destination, TIMED, original=True)
        self.assertEqual(destination.read_bytes(), raw.encode())

    def test_lrc_red_does_not_download_when_catalogue_editions_are_ambiguous(self):
        self.only(RED)
        record = dict(title=SONG[0], artist=SONG[1], album=SONG[2], duration=SONG[3])
        hits = [dict(record, isrc=code) for code in ("GBUM71029604", "GBUM71029605")]
        with patch.dict(os.environ, KARA_LRC_RED_CONTACT="public@example.org"), patch(
            "_parole_lrc_catalogues.fetch", return_value={"hits": hits}) as request:
            self.assertEqual(self.engine.LyricsSources().find(*SONG, synced=True, original_lrc=True,
                             strict=True, registry=self.index), (None, None))
        request.assert_called_once()

    def test_unison_keeps_native_lrc_and_both_credits_and_rejects_ttml(self):
        self.only(UNISON)
        raw = "[00:01.00]Premier mot  \r\n[00:04.00]Suite"
        code = "FR8EU2300090"
        item = dict(lyrics=raw, isrc=code, format="lrc", song=SONG[0], artist=SONG[1], album=SONG[2],
                    videoId="Ugr4KKK_SqY", submitter={"displayName": "Contributrice", "keyId": "public"})
        options = dict(synced=True, original_lrc=True, strict=True, registry=self.index, isrc=code, refresh=True)
        client = self.engine.LyricsSources()
        with patch("_parole_lrc_catalogues.fetch", return_value={"success": True, "data": item}) as request:
            self.assertEqual(client.find(*SONG, **options), (raw, UNISON))
        self.assertNotIn("user_agent", request.call_args.kwargs)
        self.assertEqual(load_links(self.index, sha256(raw.encode()).hexdigest()), [
            {"url": "https://unison.betterlyrics.org/song/Ugr4KKK_SqY", "label": "Paroles d’Unison"},
            {"url": "https://unison.betterlyrics.org/curator/public", "label": "Contributrice"}])
        for changed in (dict(format="ttml"), dict(album="Remix"), dict(lyrics="[00:50.00]Trop tard"),
                        dict(matchedBy="linked_video"), dict(submitter={"displayName": "Sans profil"})):
            with self.subTest(changed=changed), patch("_parole_lrc_catalogues.fetch", return_value={
                "success": True, "data": dict(item, **changed)
            }):
                self.assertEqual(client.find(*SONG, **options), (None, None))
        with patch("_parole_lrc_catalogues.fetch", side_effect=AssertionError("pas d’ISRC")):
            self.assertEqual(client.find(*SONG, **dict(options, isrc=None)), (None, None))

    def test_source_registry_upgrade_preserves_existing_unison_controls(self):
        with self.index.db() as db:
            db.execute("UPDATE sources SET category='lyrics_candidate',state='disabled' WHERE id=?", (UNISON,))
        configure_limits(self.index, UNISON, 19, 3)
        seed(self.index)
        row = next(row for row in lyrics_rows(self.index) if row["id"] == UNISON)
        self.assertEqual((row["category"], row["state"], row["remaining"], row["interval"]),
                         ("lyrics_candidate,lrc", "disabled", 19, 3))
        self.assertNotIn(UNISON, {row["id"] for row in night_lrc_providers(self.index)})

    def test_unison_checks_a_native_variant_when_the_default_is_ttml(self):
        self.only(UNISON)
        code, video = "FR8EU2300090", "Ugr4KKK_SqY"
        contributor = {"displayName": "Contributrice", "keyId": "native"}
        item = dict(id=12, lyrics="<tt>TTML</tt>", isrc=code, format="ttml", song=SONG[0],
                    artist=SONG[1], album=SONG[2], videoId=video, submitter=contributor)
        default = {"success": True, "data": item}
        listing = {"success": True, "data": [{"id": 12, "format": "ttml"}, {"id": 13, "format": "lrc"}]}
        native = dict(item, id=13, format="lrc", lyrics=TIMED)
        for value, wanted in ((native, (TIMED, UNISON)), (dict(native, isrc="FR8EU2300091"), (None, None))):
            with patch("_parole_lrc_catalogues.fetch", side_effect=[
                default, listing, {"success": True, "data": value}
            ]) as request:
                self.assertEqual(self.engine.LyricsSources().find(*SONG, synced=True, original_lrc=True,
                                 strict=True, registry=self.index, isrc=code, refresh=True), wanted)
                self.assertEqual(request.call_args.args[0], "https://unison.betterlyrics.org/lyrics/13")

    def test_community_directory_is_shared_and_download_checks_blob_and_native_metadata(self):
        name = "LRC Lyrics Library"
        self.only(name)
        raw = b"\xef\xbb\xbf[ti:Chanson]\r\n[ar:Groupe]\r\n[al:Album]\r\n[length:00:30]\r\n[00:01.00]Original \r\n"
        path = "lrc/Groupe/Album/01. Chanson.lrc"
        blob = sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        tree = {"truncated": False, "tree": [
            dict(path=path, size=len(raw), mode="100644", type="blob", sha=blob),
            dict(path="../01. Autre.lrc", size=len(raw), mode="100644", type="blob", sha=blob)]}
        options = dict(synced=True, original_lrc=True, strict=True, registry=self.index)
        with patch("_parole_lrc_catalogues.fetch", side_effect=[tree, raw]) as request:
            self.assertEqual(self.engine.LyricsSources().find(*SONG, **options), (raw.decode(), name))
            self.assertEqual(request.call_count, 2)
        with patch("_parole_lrc_catalogues.fetch", side_effect=AssertionError("absence dans le dépôt local")):
            self.assertEqual(self.engine.LyricsSources().find("Autre", *SONG[1:], **options), (None, None))
        with patch("_parole_lrc_catalogues.fetch", return_value=b"fichier remplace"):
            with self.assertRaises(self.engine.ServiceUnavailable):
                self.engine.LyricsSources().find(*SONG, **options, refresh=True)

    def test_kugou_unknown_edition_stays_in_review_until_exclusive_native_import(self):
        self.only(KUGOU)
        raw = "\ufeff[00:01.00]Premier mot  \r\n[00:04.00]Suite\r\n"
        audio = self.library / "track.flac"
        audio.write_bytes(b"audio fixture")
        token = file_token(audio)
        options = dict(synced=True, original_lrc=True, strict=True, registry=self.index,
                       recording=(audio, token))
        with patch("_parole_lrc_catalogues.fetch", return_value={"provider": "kugou", "lyrics": raw}) as request:
            with self.assertRaises(self.engine.AmbiguousLyrics):
                self.engine.LyricsSources().find(*SONG, **options)
            self.assertIn("/kugou/getLyrics?s=Chanson&a=Groupe&al=Album&d=30", request.call_args.args[0])
        with patch("_parole_lrc_catalogues.fetch", side_effect=AssertionError("candidat en cache")):
            with self.assertRaises(self.engine.AmbiguousLyrics):
                self.engine.LyricsSources().find(*SONG, **options)
        target = audio.with_suffix(".lrc")
        self.assertFalse(target.exists())
        entry, = recording_candidates(self.index.path, audio, include_text=True)["synced"]
        self.assertEqual((entry["decision"], entry["text"]), ("unverified", raw))
        song = dict(token=document(token), path=str(audio), duration=30)
        with patch.object(self.index, "song", return_value=song):
            import_lrc_candidate(self.index, "track", entry["sha256"], song["token"])
            self.assertEqual(target.read_bytes(), raw.encode())
            with self.assertRaisesRegex(ValueError, "existe déjà"):
                import_lrc_candidate(self.index, "track", entry["sha256"], song["token"])
            audio.write_bytes(b"different audio")
            with self.assertRaisesRegex(ValueError, "a changé"):
                import_lrc_candidate(self.index, "track", entry["sha256"], song["token"])

    def test_kugou_accepts_native_identity_and_rejects_a_conflicting_album(self):
        self.only(KUGOU)
        raw = "[ti:Chanson]\n[ar:Groupe]\n[al:Album]\n[length:00:30.00]\n" + TIMED
        options = dict(synced=True, original_lrc=True, strict=True, registry=self.index, refresh=True)
        with patch("_parole_lrc_catalogues.fetch", return_value={"provider": "kugou", "lyrics": raw}):
            self.assertEqual(self.engine.LyricsSources().find(*SONG, **options), (raw, KUGOU))
        with patch("_parole_lrc_catalogues.fetch", return_value={
            "provider": "kugou", "lyrics": raw.replace("[al:Album]", "[al:Autre]")
        }):
            self.assertEqual(self.engine.LyricsSources().find(*SONG, **options), (None, None))

    def test_kugou_miss_in_anonymous_cache_does_not_suspend_other_songs(self):
        self.only(KUGOU)
        headers = {"X-Provider": "kugou", "X-Auth-Mode": "cache", "X-Cache-Status": "MISS",
                   "X-RateLimit-Type": "normal"}
        response = SimpleNamespace(status=401, headers=headers, getheader=headers.get)
        connection = SimpleNamespace(request=lambda *args, **kwargs: None, getresponse=lambda: response)
        with patch("_parole_sources.wait_service"), patch(
            "_parole_sources.https_connection", return_value=nullcontext(connection)):
            self.assertEqual(self.engine.LyricsSources().find(*SONG, registry=self.index,
                             synced=True, original_lrc=True, strict=True), (None, None))
            row = next(row for row in lyrics_rows(self.index) if row["id"] == KUGOU)
            self.assertEqual((row["availability"], row["used_today"]), ("ready", 1))

    def test_lrc_red_429_keeps_retry_after_and_falls_back_without_caching_a_miss(self):
        self.only(RED, "NetEase")
        client = self.engine.LyricsSources()
        response = SimpleNamespace(status=429, headers={}, getheader=lambda key, default=None: "120")
        connection = SimpleNamespace(request=lambda *args, **kwargs: None, getresponse=lambda: response)
        with patch.dict(os.environ, KARA_LRC_RED_CONTACT="public@example.org"), patch(
            "_parole_sources.wait_service"), patch("_parole_sources.defer_service"), patch(
            "_parole_sources.https_connection", return_value=nullcontext(connection)), patch.object(
            client.netease, "find", return_value=TIMED):
            self.assertEqual(client.find(*SONG, synced=True, original_lrc=True, strict=True,
                                         registry=self.index), (TIMED, "NetEase"))
            row = next(row for row in lyrics_rows(self.index) if row["id"] == RED)
        self.assertEqual((row["availability"], row["last_http"], row["used_today"]), ("limited", 429, 1))
        self.assertGreater(row["next_available"], time() + 115)
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reviews WHERE kind=? AND state='absent'",
                                        (CACHE_KIND,)).fetchone()[0], 0)

    def test_timed_lookup_prioritizes_clean_edition_and_refreshes_only_old_misses(self):
        from _parole_lyrics_lookup import cache_identity, cache_value, remember

        self.only("LRCLIB")
        song = ("03 - L’Été (feat. Invité)", "Artiste, Invité", "Édition", 30, "Artiste")
        config = lyrics_rows(self.index)[0]["config"]
        key = cache_identity("LRCLIB", (*song, ""), True, "", True, config, original_lrc=True)
        remember(self.index, key, cache_value(None))
        client = self.engine.LyricsSources()
        record = dict(id=800, trackName="L’Été", artistName="Artiste, Invité",
                      albumName="Édition", duration=30, syncedLyrics=TIMED)

        def lookup(endpoint, **terms):
            if endpoint == "get":
                return None
            self.assertEqual(terms, {"track_name": "L’Été", "artist_name": "Artiste", "album_name": "Édition"})
            return [dict(record, id=801, albumName="Autre édition"), record]

        with patch.object(client.lrc, "get", side_effect=lookup) as query:
            self.assertEqual(client.find(*song, synced=True, original_lrc=True, strict=True, registry=self.index),
                             (TIMED, "LRCLIB"))
            self.assertEqual(query.call_count, 2)
        with patch.object(client.lrc, "get", side_effect=AssertionError("LRC déjà vérifié")):
            self.assertEqual(client.find(*song, synced=True, original_lrc=True, strict=True, registry=self.index),
                             (TIMED, "LRCLIB"))

    def test_exact_plain_lookup_populates_native_cache_before_quota_expires(self):
        self.only("LRCLIB")
        raw = "\ufeff[00:01.00]Premier mot  \r\n"
        record = dict(trackName=SONG[0], artistName=SONG[1], albumName=SONG[2], duration=30,
                      plainLyrics="Premier mot", syncedLyrics=raw)
        client = self.engine.LyricsSources()
        with patch.object(client.lrc, "get", return_value=record) as query:
            self.assertEqual(client.find(*SONG, synced=False, strict=True, registry=self.index),
                             ("Premier mot", "LRCLIB"))
        query.assert_called_once()
        configure_limits(self.index, "LRCLIB", 0, 0)
        other = self.engine.LyricsSources()
        with patch.object(other.lrc, "get", side_effect=AssertionError("quota local en pause")):
            self.assertEqual(other.find(*SONG, synced=True, original_lrc=True, strict=True, registry=self.index),
                             (raw, "LRCLIB"))

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
            self.assertEqual([row["id"] for row in lyrics_rows(self.index)][:2], [RED, "LRCLIB"])
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

    def test_lrc_equivalence_requires_each_line_and_bounded_spread(self):
        choose = self.engine.choose_transcript
        first = "[00:01.00]Premier texte\n[00:03.00]Deuxième ligne"
        close = "[00:01.08]Premier texte\n[00:03.03]Deuxième ligne"
        self.assertIn(choose([(first, {}), (close, {})], True, True), (first, close))
        for other in (close.replace("03.03", "03.11"), close.replace("Deuxième", "Dernière"),
                      close.splitlines()[0]):
            with self.assertRaises(self.engine.AmbiguousLyrics):
                choose([(first, {}), (other, {})], True, True)
        marks = first.replace("Premier texte", "<00:01.00>Premier <00:01.50>texte")
        self.assertEqual(choose([(first, {}), (marks, {})], True, True), marks)
        with self.assertRaises(self.engine.AmbiguousLyrics):
            choose([(marks, {}), (marks.replace("01.50", "01.76"), {})], True, True)

    def test_artist_outcomes_are_exclusive_and_provider_yield_counts_publications(self):
        from _parole_import_enrichment import ARTIST_OUTCOMES, artist_lrc_summary, artist_summary

        cases = (("present", "present"), ("found", "limited"), ("found", "found"), ("present", "review"),
                 ("missing", "missing"), ("missing", "limited"), ("temporary", "missing"))
        audit = [{"type": "request", "provider": "LRCLIB", "sent": True},
                 {"type": "provider", "provider": "LRCLIB", "outcome": "found"},
                 {"type": "publication", "provider": "LRCLIB", "form": "lrc"}]
        rows = {i: {"lyrics": plain, "lrc": timed} for i, (plain, timed) in enumerate(cases)}
        rows[2]["audit"] = audit
        self.assertEqual(artist_summary(rows), (dict.fromkeys(ARTIST_OUTCOMES, 1),
                         {"LRCLIB": {"requests": 1, "found": 1, "published_lrc": 1}}))
        self.assertEqual(artist_lrc_summary(rows),
                         {"present": 1, "limited": 2, "found": 1, "review": 1, "missing": 2})


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
        self.only()
        self.assertEqual([row["id"] for row in candidates(database, *SONG)], [800])
        self.assertEqual(candidates(database, "Chanson", "Groupe", "Edition live", 30, "Groupe"), [])
        with patch.dict(os.environ, {"PAROLE_LRCLIB_SQLITE": str(database)}):
            value, source = self.engine.LyricsSources().find(*SONG, synced=True, registry=self.index)
            original, original_source = self.engine.LyricsSources().find(
                *SONG, synced=True, registry=self.index, original_lrc=True)
            self.assertEqual(night_lrc_providers(self.index),
                             [dict(id="LRCLIB local", availability="ready", next_available=None)])
        self.assertEqual((original, original_source), (TIMED, "LRCLIB local"))
        self.assertEqual(source, "LRCLIB local")
        self.assertIn("<00:01.804>", value)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(list(self.base.glob("snapshot.sqlite3-*")), [])


if __name__ == "__main__":
    unittest.main()
