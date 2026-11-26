#!/usr/bin/python3 -B
"""Fixtures locales, sans lecture ni capture audio et sans compte réseau réel."""
from hashlib import sha256
from pathlib import Path
from contextlib import closing
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from time import time
from threading import Event
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from _kara_audio_identity import pcm_digest
from _kara_prepare import lrc_token
from _parole_catalogue import check_album, check_discography, check_recording, compare
from _parole_codex import authenticate, daily_slot, validate
from _parole_index import Index, inventory, encoded_key
from _parole_maintenance import Journal, Maintenance
from _parole_process import Interrupted, run
from _parole_sources import LyricsEndpoint, discover, external_ips, public_url, seed, controls, validate_endpoint
from _parole_work import lyrics_engine, save_correction

ARTIST = "aaaaaaaa-0000-0000-0000-000000000001"
RELEASE = "aaaaaaaa-0000-0000-0000-000000000002"
GROUP = "aaaaaaaa-0000-0000-0000-000000000003"
OTHER = "aaaaaaaa-0000-0000-0000-000000000004"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "music"
        self.root.mkdir()
        self.index = Index(self.root, self.base / "state")

    def audio(self, name="01 Chanson.flac", *, title="Chanson", lyrics=""):
        path = self.root / "Groupe" / "Album" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", "5", "-metadata", f"title={title}", "-metadata", "artist=Groupe, Invité",
                        "-metadata", "album_artist=Groupe", "-metadata", "album=Album",
                        "-metadata", "track=1", "-metadata", f"lyrics={lyrics}", str(path)], check=True)
        return path

    def scan(self, **kwargs):
        return inventory(self.index, kwargs.pop("db", None), lambda *_: ("", "absent", {}),
                         lambda: False, lambda *_: None, **kwargs)

    def songs(self):
        with self.index.db() as db:
            return db.execute("SELECT * FROM songs ORDER BY path").fetchall()


class InventoryTests(Fixture):
    def test_manual_correction_in_lexicon_is_bound_to_sidecar_and_audio(self):
        from _parole_lexicon import records

        path = self.audio()
        self.scan()
        song, = self.songs()
        with self.assertRaisesRegex(ValueError, "l’ouverture"):
            save_correction(self.index, song["id"], ".txt", None, "J’suis chez Mélo",
                            expected_audio="old-version")
        save_correction(self.index, song["id"], ".txt", None, "J’suis chez Mélo", expected_audio=song["token"])
        self.scan()
        record, = records(self.index.path, self.root)
        self.assertEqual(record["source"], "Correction manuelle validée")
        self.assertIn("J’suis", record["forms"])
        path.with_suffix(".txt").write_text("Mots jamais validés")
        self.scan()
        self.assertEqual(list(records(self.index.path, self.root)), [])

    def test_validation_receipts_preserve_the_origin_of_each_format(self):
        self.audio()
        self.scan()
        song, = self.songs()
        save_correction(self.index, song["id"], ".txt", None, "Texte saisi manuellement")
        save_correction(self.index, song["id"], ".lrc", None, "[00:01.000]Brouillon corrigé", draft=True)
        with self.index.db() as db:
            rows = db.execute("""SELECT substr(subject,-4),json_extract(value,'$.source') FROM reviews
                                WHERE kind='transcription_validation' AND state='validated'""")
            self.assertEqual(dict(rows), {".txt": "Correction manuelle", ".lrc": "Turbo, validé manuellement"})

    def test_lexicon_excludes_held_out_content_and_generated_lrc(self):
        from _parole_lexicon import records, select_prompt

        paths = [self.audio(f"{title}.flac", title=title) for title in ("test", "duplicate", "generated", "training")]
        for path, text in zip(paths, ("Texte témoin", "Texte témoin!", "Copie Turbo", "Wèche, j'suis chez Mélo.")):
            path.with_suffix(".lrc").write_text(f"[00:01.00]{text}\n")
        self.scan()
        group = {Path(row["path"]).stem: row for row in self.songs()}
        self.index.update(group["generated"]["id"], lrc="généré")
        corpus = list(records(self.index.path, self.root, exclude=(group["test"]["id"],)))
        self.assertEqual([row["song"] for row in corpus], [group["training"]["id"]])
        self.assertIn("j'suis", corpus[0]["forms"])
        self.assertIn("Mélo", select_prompt(corpus, artist="Groupe"))

    def test_incremental_rename_and_disappearance(self):
        path = self.audio()
        path.with_suffix(".lrc").write_text("[00:01.00]Tout commence ici\n[00:02.00]Et continue là\n")
        self.assertEqual(self.scan()["changed"], 1)
        row = self.songs()[0]
        self.assertEqual((row["artist"], row["owner"], row["lrc"], row["track"]),
                         ("Groupe, Invité", "Groupe", "local", 1))
        self.assertEqual(self.scan(reader=lambda _: self.fail("probe sur fichier inchangé"))["changed"], 0)
        moved = path.with_name("Renommée.flac")
        path.rename(moved)
        path.with_suffix(".lrc").rename(moved.with_suffix(".lrc"))
        self.scan()
        self.assertEqual((self.songs()[0]["id"], self.songs()[0]["previous"]), (row["id"], str(path)))
        moved.unlink()
        self.scan(limit=1)
        self.assertEqual(len(self.songs()), 1)
        self.assertEqual(self.scan()["missing"], 1)

    def test_read_only_strawberry_and_lrc_correction(self):
        song = self.audio()
        db = self.base / "collection.db"
        st = song.stat()
        with closing(sqlite3.connect(db)) as source, source:
            source.execute("CREATE TABLE songs (url,title,artist,album,albumartist,lyrics,mtime,filesize,length,unavailable)")
            source.execute("INSERT INTO songs VALUES(?,?,?,?,?,?,?,?,?,0)",
                           (song.as_uri(), "Chanson", "Groupe, Invité", "Album", "Groupe",
                            "Tout commence ici", int(st.st_mtime), st.st_size, 5_000_000_000))
        before = sha256(db.read_bytes()).digest()
        self.scan(db=db)
        row = self.songs()[0]
        self.assertEqual(row["lyrics"], "Strawberry")
        self.assertEqual(json.loads(row["info"])["plain"], "Tout commence ici")
        self.assertEqual(sha256(db.read_bytes()).digest(), before)
        with closing(sqlite3.connect(db)) as source, source:
            source.execute("ALTER TABLE songs ADD COLUMN genre")
            source.execute("UPDATE songs SET genre='Rap français'")
        self.scan(db=db)
        self.assertEqual(json.loads(self.songs()[0]["info"])["tags"]["genre"], "Rap français")
        save_correction(self.index, row["id"], ".lrc", None, "[00:01.000]Tout commence ici")
        version = lrc_token(song.with_suffix(".lrc"))
        with self.assertRaises(ValueError):
            save_correction(self.index, row["id"], ".lrc", None, "[00:01.500]Correction")
        save_correction(self.index, row["id"], ".lrc", version, "[00:01.500]Correction")
        backups = list((self.index.folder / "corrections" / row["id"]).glob("*.lrc"))
        self.assertEqual(len(backups), 1)
        self.assertIn("Tout commence", backups[0].read_text())

    def test_manual_group_only_applies_to_album(self):
        folder = self.base / "state/kara/music-tidy-backups/snapshot"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"status": "complete", "files": [
            {"source": "Groupe, Invité/Album/Chanson.flac", "destination": "Groupe/Album/Chanson.flac",
             "changes": {"albumartist": "Groupe"}},
            {"source": "../Album/x.flac", "destination": "Groupe/Album/x.flac", "changes": {"albumartist": "Groupe"}}]}))
        self.assertEqual(self.index.associations(self.base / "state"), 1)
        with self.index.db() as db:
            row, = db.execute("SELECT * FROM associations").fetchall()
            self.assertEqual(tuple(row)[:3], ("Groupe, Invité", "Album", "Groupe"))

    def test_pcm_identity_survives_tag_edit_but_changes_for_real_pcm_change(self):
        path = self.audio()
        other = path.with_name("Retag.flac")
        changed = path.with_name("Décalé.flac")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-c:a", "copy",
                        "-metadata", "album_artist=Autre", str(other)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", "3", str(changed)], check=True)
        self.assertEqual(pcm_digest(path), pcm_digest(other))
        self.assertNotEqual(pcm_digest(path), pcm_digest(changed))


class QueueTests(Fixture):
    def test_classic_queue_ignores_legacy_global_quota_and_counts_external_publications(self):
        owner = Maintenance(self.root, None, state=self.base / "state")
        self.addCleanup(owner.close)
        index = owner.index
        index.enqueue("lyrics", "song", "fixture", now=10)
        self.assertTrue(index.reserve_daily("network", 1, now=10, gap=20))
        owner.execute = Mock()
        self.assertTrue(owner.tick_network(index.settings(), 11, ("lyrics",)))
        task = owner.execute.call_args.args[0]
        with patch("_parole_index.time", return_value=12):
            index.finish(task, "done", "found", network_found=True)
            index.finish(task, "done", "found", network_found=True)
        self.assertEqual(index.network_yield(now=120), {"lyrics": 1})
        self.assertEqual(index.network_yield(now=3660), {})

    def test_activity_intervals_and_night_count_only_settled_results(self):
        start = 1_800_000_000
        with patch("_parole_index.time", return_value=start):
            night = self.index.start_closed_report()
        for kind, source, stamp, state, activity in (
            ("lyrics", "external", start + 5, "done", ()),
            ("lrc", "local", start + 6, "done", ("local_lrc",)),
            ("lrc", "quota", start + 61, "temporary", ("quota",)),
        ):
            self.index.enqueue(kind, source, "revision", now=stamp)
            task = self.index.claim((kind,), stamp)
            with patch("_parole_index.time", return_value=stamp):
                for _ in range(2):
                    self.index.finish(task, state, delay=300, closed=(night, "completed"),
                                      network_found=source == "external", activity=activity)
        with patch("_parole_index.time", return_value=start + 62):
            for _ in range(2):
                self.index.record_validation("draft", ".txt", "audio", "same sidecar", draft=True)
        hour, day, drafts = self.index.activity_progress(now=start + 3660)
        self.assertEqual((hour["network_lyrics"], hour["local_lrc"], hour["quota"], hour["validated_lyrics"], drafts),
                         (0, 0, 1, 1, 0))
        self.assertEqual(day, {"network_lyrics": 1, "local_lrc": 1, "quota": 1, "validated_lyrics": 1})
        self.assertEqual(self.index.closed_progress()["activity"], {"network_lyrics": 1, "local_lrc": 1, "quota": 1})
        self.assertEqual(self.index.activity_progress(now=start + 25 * 3600), ({}, {}, 0))

    def test_network_batch_settles_fast_track_while_another_waits(self):
        owner = Maintenance(self.root, None, state=self.base / "state")
        self.addCleanup(owner.close)
        index = owner.index
        owner.current_settings = index.settings()
        for kind, subject in (("lrc", "same"), ("lyrics", "same"), ("lyrics", "fast")):
            index.enqueue(kind, subject, "fixture", now=5)
        fast_settled = Event()

        def output(task, settings, *, stop):
            if task["subject"] == "same" and not fast_settled.wait(2):
                raise TimeoutError("La sortie rapide n’a pas pu être traitée")
            return {"state": "found", "source": "LRCLIB"}

        def result(task, _value):
            if task["subject"] == "fast":
                fast_settled.set()
            return "done", "found", 0

        owner.network_output = output
        owner.network_result = result
        self.assertTrue(owner.tick_network(owner.current_settings, 6, ("lrc", "lyrics")))
        with index.db() as db:
            states = {(r["kind"], r["subject"]): r["state"] for r in db.execute("SELECT * FROM tasks")}
        self.assertEqual(states, {("lrc", "same"): "done", ("lyrics", "same"): "queued", ("lyrics", "fast"): "done"})
        self.assertEqual(owner.network_width, 3)
        self.assertFalse(owner.network_active)

    def test_automatic_codex_is_opt_in_and_manual_claims_remain_available(self):
        self.assertFalse(self.index.settings()["codex_auto"])
        self.index.enqueue("codex", "automatic", "rev", priority=10, now=5)
        self.index.enqueue("codex", "manual", "rev", priority=15, now=5, payload={"manual": True})
        self.assertEqual(self.index.claim(("codex",), 10, codex_auto=False)["subject"], "manual")
        self.assertIsNone(self.index.claim(("codex",), 10, codex_auto=False))
        self.assertEqual(self.index.claim(("codex",), 10, codex_auto=True)["subject"], "automatic")

    def test_resume_dedupe_cancel_and_budget(self):
        self.index.enqueue("lyrics", "song", "v1", now=100.)
        self.index.enqueue("lyrics", "song", "v1", now=101.)
        task = self.index.claim(("lyrics",), 102., bucket="network", limit=2)
        self.assertEqual(task["attempts"], 1)
        self.assertIsNone(self.index.claim(("lyrics",), 103.))
        self.index.recover()
        task = self.index.claim(("lyrics",), 104., bucket="network", limit=2)
        self.assertEqual(task["attempts"], 2)
        self.index.cancel("lyrics", "song")
        self.index.finish(task, "done")
        self.index.enqueue("lyrics", "song", "v1", now=105.)
        self.assertIsNone(self.index.claim(("lyrics",), 106.))
        self.index.enqueue("lyrics", "other", "v1", now=105.)
        self.assertIsNone(self.index.claim(("lyrics",), 106., bucket="network", limit=2))
        self.assertEqual(self.index.claim(("lyrics",), 100_000., bucket="network", limit=2)["subject"], "other")

    def test_journal_masks_credentials_even_in_catalogue_text(self):
        journal = Journal(self.index.folder)
        self.addCleanup(journal.close)
        journal.emit("CODEX", "Album https://example.org/secret-track token: NEVER_DISPLAY")
        journal.emit("DOWNLOAD", "Cookie: NEVER_PERSIST")
        self.assertNotIn("NEVER_", repr(journal.snapshot()))
        self.assertNotIn("secret-track", journal.path.read_text())
        self.assertNotIn("NEVER_", repr(journal.history()))

    def test_supervised_cancellation_and_timeout(self):
        stop_after = time() + .15
        with self.assertRaises(Interrupted):
            run([sys.executable, "-c", "import time; time.sleep(60)"],
                stop=lambda: time() >= stop_after, timeout=2)
        with self.assertRaises(TimeoutError):
            run([sys.executable, "-c", "import time; time.sleep(60)"], stop=lambda: False, timeout=.15)
        with self.assertRaises(ValueError):
            run([sys.executable, "-c", "print('x' * 10000)"], stop=lambda: False, timeout=3, maximum=100)


def official(first="Tout commence ici"):
    return {"id": RELEASE, "status": "Official", "title": "Album", "date": "2024-04-02",
            "artist-credit": [{"name": "Groupe", "artist": {"id": ARTIST}}],
            "release-group": {"id": GROUP},
            "media": [{"position": 1, "track-count": 1, "tracks": [{"position": 1, "title": first}]},
                      {"position": 2, "track-count": 1, "tracks": [{"position": 1, "title": "Final"}]}]}


class CatalogueTests(unittest.TestCase):
    first = dict(disc=1, track=1, title="Tout commence ici", owner="Groupe", album="Album",
                 artist_id=ARTIST, release_id=RELEASE, year=2024)
    second = dict(disc=2, track=1, title="Final")

    def test_real_position_and_multidisc(self):
        self.assertEqual(compare([self.first, self.second], official())[0], "complete")
        status, details = compare([self.first, self.first], official())
        self.assertEqual(status, "incomplete")
        self.assertEqual(details["missing"], [[2, 1, "Final"]])
        self.assertEqual(len(details["duplicates"]), 1)
        self.assertEqual(compare([{**self.first, "disc": 0}], official())[0], "ambiguous")

    def test_recording_requires_edition_duration_and_unambiguous_credit(self):
        expected = {"id": OTHER, "title": "Titre", "length": 45_100,
                    "artist-credit": [{"name": "Groupe", "artist": {"id": ARTIST}}],
                    "releases": [{"title": "Album", "id": RELEASE, "status": "Official"}]}
        tags = {"artist": "Groupe", "album": "Album"}
        def lookup(*_args, **_kwargs):
            return {"count": 1, "recordings": [expected]}
        state, found = check_recording(tags, "Titre", "Groupe", "Album", 45, lookup)
        self.assertEqual((state, found["recording_id"]), ("identified", OTHER))
        self.assertEqual(check_recording(tags, "Homonyme", "Groupe", "Album", 45, lookup)[0], "not_found")
        self.assertEqual(check_recording(tags, "Titre", "Groupe", "Album", 60, lookup)[0], "not_found")
        self.assertEqual(check_recording(tags, "Titre", "Groupe", "Deluxe", 45, lookup)[0], "not_found")

    def test_edition_verification_homonymous_discography(self):
        status, details = check_album([self.first], (), lambda path, **_: official())
        self.assertEqual((status, details["verified"]), ("incomplete", True))
        ambiguous = {**self.first, "release_id": ""}
        self.assertEqual(check_album([ambiguous], (), lambda p, **_: {"releases": [official()], "count": 1}
                                     if p == "release" else official())[0], "ambiguous")
        self.assertEqual(check_discography("", set(), set(), ["Album"])[0], "ambiguous")
        state, details = check_discography(ARTIST, {GROUP}, {"album"}, ["Album"], lambda p, **_: {
            "release-group-count": 4, "release-groups": [{"id": GROUP, "title": "Album", "primary-type": "Album"},
            {"id": OTHER, "title": "Absent", "primary-type": "Album", "first-release-date": "2020"},
            {"id": RELEASE, "title": "Live", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2020"},
            {"id": RELEASE, "title": "Collaboration", "primary-type": "Album", "first-release-date": "2022",
             "artist-credit": [{"artist": {"id": ARTIST}}, {"artist": {"id": OTHER}}]}]})
        self.assertEqual((state, [r["title"] for r in details["missing"]]), ("checked", ["Absent"]))


class SourceTests(Fixture):
    def test_access_denial_is_not_negative_cache_and_is_local_to_provider(self):
        from contextlib import nullcontext
        from urllib.error import HTTPError
        from _parole_import_metrics import snapshot
        from _parole_sources import recent_source

        seed(self.index)
        with self.index.db() as db:
            db.execute("UPDATE sources SET state='disabled' WHERE id NOT IN ('NetEase', 'QQ Music')")
        engine = lyrics_engine()
        clients = engine.LyricsSources()
        denied = HTTPError("https://music.163.com/", 403, "Forbidden", {"Retry-After": "380"}, None)
        song = ("Chanson", "Groupe", "Album", 30, "Groupe")
        # No real quota or HTTP call is needed to exercise the routing,
        # durable episode, fallback and negative-cache behavior together.
        with patch("_parole_sources.wait_service"), patch.object(
                engine, "resource_slot", side_effect=lambda *_args, **_kw: nullcontext()), patch.object(
                engine, "defer_service") as pause, patch.object(
                clients.netease.opener, "open", side_effect=denied) as transport, patch.object(
                clients.qqmusic, "find", return_value="Texte vérifié"):
            self.assertEqual(clients.find(*song, synced=False, strict=True, registry=self.index),
                             ("Texte vérifié", "QQ Music"))
            pause.assert_called_once_with("music.163.com", 380)
            self.assertEqual(snapshot(self.index)["NetEase"]["last_http"], 403)
            self.assertEqual(recent_source(self.index, "NetEase")["résultats"], {"error": 1})
            with self.index.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM reviews WHERE kind='lyrics_query_v1'").fetchone()[0], 1)
            self.assertEqual(clients.find(*song, synced=False, strict=True, registry=self.index),
                             ("Texte vérifié", "QQ Music"))
            transport.assert_called_once()

    def test_lyrics_ovh_distinguishes_missing_text_and_denied_access(self):
        from urllib.error import HTTPError

        engine = lyrics_engine()
        client = engine.LyricsOvh()
        with patch.object(client.gate, "wait", return_value=True), patch.object(
                client.gate, "defer") as pause, patch.object(client.opener, "open") as transport:
            client.failure = None
            transport.side_effect = HTTPError("https://api.lyrics.ovh/", 404, "Not found", {}, None)
            self.assertIsNone(client.get("v1/Groupe/Chanson"))
            self.assertIsNone(client.failure)
            pause.assert_not_called()
            transport.side_effect = HTTPError("https://api.lyrics.ovh/", 451, "Denied", {}, None)
            self.assertIsNone(client.get("v1/Groupe/Chanson"))
            self.assertEqual(client.failure.reason, "unavailable")
            pause.assert_called_once_with("api.lyrics.ovh", 300, 451)

    def test_exact_pcm_reference_crosses_editions_without_network_or_retag(self):
        from _parole_local_reference import SOURCE, local_reference
        from _parole_index import LOCAL_REFERENCE_MISS, sidecar
        from _parole_work import lookup_song

        original = self.audio("album.flac")
        target = self.audio("anthologie.flac")
        # The two containers have independent editions and identical decoded PCM.
        anthology = target.parent.parent / "Anthologie"
        anthology.mkdir()
        target = target.rename(anthology / target.name)
        source = original.with_suffix(".lrc")
        source.write_text("[00:01.000]Chant contrôlé\n")
        self.scan()
        by_path = {Path(song["path"]): song for song in self.songs()}
        first, second = by_path[original], by_path[target]
        self.assertNotEqual(first["edition"], second["edition"])
        self.assertEqual(pcm_digest(original), pcm_digest(target))
        for song in by_path.values():
            self.index.update(song["id"], audio=pcm_digest(Path(song["path"])))
        before = target.read_bytes()
        self.assertIsNone(local_reference(self.index, self.index.song(second["id"]), synced=True))
        owner = Maintenance(self.root, None, state=self.base / "state")
        self.addCleanup(owner.close)
        owner.output = Mock(wraps=owner.output)
        with self.index.db() as db:
            db.execute("""INSERT INTO daily_usage(bucket,day,used)
                          SELECT 'provider:'||id,date('now','localtime'),200 FROM sources""")
        settings = owner.current_settings = self.index.settings()
        self.index.enqueue("lrc", second["id"], "fixture")
        kinds = owner.network_kinds(settings, time())
        self.assertNotIn("lrc", kinds)
        self.assertTrue(owner.tick_network(settings, time(), kinds))
        self.assertEqual(owner.output.call_args.args[0][-1], "--local-reference-only")
        with self.index.db() as db:
            self.assertEqual(tuple(db.execute("SELECT state,detail,attempts FROM tasks").fetchone()),
                             ("temporary", LOCAL_REFERENCE_MISS, 0))
        self.assertFalse(self.index.work_due(("lrc",), time(), local_reference=True))
        self.assertTrue(self.index.work_due(("lrc",), time()))
        self.assertFalse(target.with_suffix(".lrc").exists())

        self.index.review("transcription_validation", first["id"] + ".lrc", "validated",
                          {"audio": first["token"], "sidecar": sidecar(original, ".lrc")[0]})
        self.index.update(first["id"], checked=time())
        engine = lyrics_engine()
        with patch("_parole_work.lyrics_engine", return_value=engine), patch.object(
                engine.LyricsSources, "find", side_effect=AssertionError("requête superflue")):
            preview = lookup_song(self.index, second["id"], True, read_only=True)
            self.assertEqual((preview["source"], preview["lyrics"]), (SOURCE, source.read_text()))
            self.assertFalse(target.with_suffix(".lrc").exists())
            # Reuse plain text and exact timestamps in separate publications.
            self.assertEqual(lookup_song(self.index, second["id"], False)["source"], SOURCE)
        self.assertTrue(owner.tick_network(settings, time(), kinds))
        self.assertEqual(owner.output.call_args.args[0][-1], "--local-reference-only")
        with self.index.db() as db:
            self.assertEqual(tuple(db.execute("SELECT state,detail FROM tasks").fetchone()),
                             ("done", "référence PCM locale"))
        self.assertFalse(self.index.network_yield())
        self.assertEqual(target.with_suffix(".txt").read_text(), "Chant contrôlé\n")
        self.assertEqual(target.with_suffix(".lrc").read_bytes(), source.read_bytes())
        self.assertEqual(target.read_bytes(), before)
        source.write_text("[00:01.250]Correction plus récente\n")
        self.assertIsNone(local_reference(self.index, self.index.song(second["id"]), synced=True))

    def test_embedded_lrc_is_materialized_on_a_local_turn_with_or_without_provider_slots(self):
        embedded = "[00:01.000]Vers déjà présent"
        for available in (True, False):
            with self.subTest(provider_slots=available):
                path = self.audio(f"embedded-{available}.flac", lyrics=embedded)
                self.scan()
                song = next(row for row in self.songs() if row["path"] == str(path))
                self.assertEqual((song["lrc"], song["audio"]), ("intégré", ""))
                before = path.read_bytes()
                self.index.enqueue("lrc", song["id"], "fixture")
                owner = Maintenance(self.root, None, state=self.base / "state")
                self.addCleanup(owner.close)
                owner.output = Mock(wraps=owner.output)
                if not available:
                    with self.index.db() as db:
                        db.execute("""INSERT INTO daily_usage(bucket,day,used)
                                      SELECT 'provider:'||id,date('now','localtime'),200 FROM sources""")
                settings = owner.current_settings = self.index.settings()
                kinds = owner.network_kinds(settings, time())
                self.assertEqual("lrc" in kinds, available)
                self.assertTrue(self.index.work_due(("lrc",), time(), local_reference=True))
                self.assertTrue(owner.tick_network(settings, time(), kinds))
                self.assertEqual(owner.output.call_args.args[0][-1], "--local-reference-only")
                self.assertEqual(path.with_suffix(".lrc").read_text(), embedded + "\n")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.index.song(song["id"])["lrc"], "local")
                self.assertFalse(self.index.work_due(("lrc",), time(), local_reference=True))
                self.assertFalse(self.index.network_yield())
        _, day, _ = self.index.activity_progress(now=time() + 60)
        self.assertEqual(day, {"local_lrc": 2})

    def test_unison_exact_isrc_keeps_attribution_and_discards_lyrics(self):
        from _parole_unison import discover

        seed(self.index)
        payload = {"success": True, "data": {"isrc": "FR8EU2300090", "matchedBy": "isrc",
                                              "videoId": "Ugr4KKK_SqY", "format": "lrc",
                                              "lyrics": "Texte non conservé",
                                              "submitter": {"keyId": "abcd", "displayName": "Crédit"}}}
        song = {"id": "fixture", "token": "audio"}
        with patch("_parole_unison.fetch", return_value=payload) as request:
            result = discover(self.index, song, "fr-8eu-23-00090", lambda _: True)
            self.assertEqual(result["profile"], "https://unison.betterlyrics.org/curator/abcd")
            self.assertNotIn("lyrics", result)
            self.assertEqual(discover(self.index, song, "FR8EU2300090", lambda _: True), result)
            self.assertIsNone(discover(self.index, song, "FR8EU2300091", lambda _: True))
            self.assertEqual(request.call_count, 2)

    def test_lrclib_widens_discovery_without_widening_recording_match(self):
        engine = lyrics_engine()
        client = engine.LrcLib()
        client.strict = True
        record = dict(trackName="Titre", artistName="Artiste, Invité", albumName="Album",
                      duration=10, syncedLyrics="[00:01.000]Paroles entendues")
        client.get = Mock(side_effect=(None, [dict(record, albumName="Compilation")], [record]))
        self.assertEqual(client.find("Titre", "Artiste, Invité", "Album", 10, "Artiste"), "Paroles entendues")
        self.assertEqual(client.get.call_args.kwargs["artist_name"], "Artiste, Invité")

    def test_lrclib_reports_other_edition_only_after_other_evidence_matches(self):
        engine = lyrics_engine()
        client = engine.LrcLib()
        record = dict(trackName="Titre", artistName="Artiste", albumName="Anthologie",
                      duration=12, syncedLyrics="[00:01.000]Paroles entendues")
        client.get = Mock(side_effect=(None, [record]))
        self.assertIsNone(client.find("Titre", "Artiste", "Album", 12, "Artiste", synced=True))
        self.assertTrue(client.edition_candidate)
        client.get = Mock(side_effect=(None, [dict(record, duration=22)]))
        self.assertIsNone(client.find("Titre", "Artiste", "Album", 12, "Artiste", synced=True))
        self.assertFalse(client.edition_candidate)

    def test_source_recent_latency_is_bounded_and_can_be_reset(self):
        from _parole_sources import recent_source, record

        seed(self.index)
        record(self.index, "LRCLIB", "different_edition", .5)
        self.assertIsNone(recent_source(self.index, "LRCLIB")["p95_ms"])
        for number in range(1, 136):
            record(self.index, "LRCLIB", "absent", number / 1000)
        result = recent_source(self.index, "LRCLIB")
        self.assertEqual(result["mesures"], 128)
        self.assertEqual(result["p95_ms"], 129)
        self.assertEqual(result["résultats"], {"absent": 128})
        controls(self.index, "LRCLIB", "reset")
        self.assertIsNone(recent_source(self.index, "LRCLIB"))

    def test_unavailable_catalogue_leaves_lyrics_eligible(self):
        from _parole_import_metrics import limited
        from _parole_sources import ready_categories, seed

        seed(self.index)
        now = time()
        self.assertIn("musicbrainz", ready_categories(self.index, now))
        limited(self.index, "MusicBrainz", 30, 503, now=now)
        ready = ready_categories(self.index, now)
        self.assertNotIn("musicbrainz", ready)
        self.assertIn("lyrics", ready)
        self.assertIn("musicbrainz", ready_categories(self.index, now + 30))

    def test_maintenance_queries_use_track_review_and_fall_back_to_tags(self):
        self.audio()
        self.scan()
        song, = self.songs()
        maint = Maintenance(self.root, None, state=self.base / "state")
        self.addCleanup(maint.close)
        self.assertEqual(maint.query("lyrics", song["id"])["release_id"], song["release_id"])
        self.assertEqual(maint.query("track", song["edition"])["missing_tracks"], [])
        maint.index.review("album", song["edition"], "incomplete", {"id": RELEASE, "missing": [[1, 2, "Outro"]]})
        query = maint.query("track", song["edition"])
        self.assertEqual(query["release_id"], RELEASE)
        self.assertEqual(query["missing_tracks"], [[1, 2, "Outro"]])

    def test_provider_quotas_and_intervals_are_independent(self):
        from _parole_sources import ProviderUnavailable, configure_limits, wait_provider

        seed(self.index)
        configure_limits(self.index, "LRCLIB", 1, 6)
        configure_limits(self.index, "MusicBrainz", 2, 0)
        with patch("_parole_sources.wait_service") as host:
            wait_provider(self.index, "LRCLIB", "lrclib.net", .75)
            host.assert_called_with("lrclib.net", 6, max_wait=5)
            with self.assertRaises(ProviderUnavailable):
                wait_provider(self.index, "LRCLIB", "lrclib.net", .75)
            wait_provider(self.index, "MusicBrainz", "musicbrainz.org", 1.2)
            host.assert_called_with("musicbrainz.org", 1.2, max_wait=5)
            controls(self.index, "MusicBrainz", "disabled")
            host.reset_mock()
            with self.assertRaises(ProviderUnavailable):
                wait_provider(self.index, "MusicBrainz", "musicbrainz.org", 1.2)
            host.assert_not_called()

    def response(self, source):
        return {"research_id": "id", "identification": "Chanson", "status": "found", "sources": [source],
                "missing": [], "matches": [], "contradictions": [], "confidence": "high", "proposal": "", "restrictions": []}

    def source(self, url="https://lyrics.example.org/api/search"):
        return dict(url=url, documentation="https://lyrics.example.org/docs",
                    terms="https://lyrics.example.org/terms", category="lrc", adapter="lrclib")

    def test_discovered_import_adapter_stays_pending(self):
        maint = Maintenance(self.root, None, state=self.base / "state")
        self.addCleanup(maint.close)
        self.assertFalse(maint.queue_provider_discovery("Artiste / Album"))
        maint.index.configure(codex=True)
        maint.current_settings = maint.index.settings()
        self.assertTrue(maint.queue_provider_discovery("Artiste / Album"))
        with maint.index.db() as db:
            task = db.execute("SELECT subject,payload FROM tasks WHERE kind='codex'").fetchone()
        query = json.loads(task["payload"])
        self.assertTrue(query["manual"])
        self.assertEqual(query["kind"], "provider_search")
        with self.assertRaises(ValueError):
            validate(self.response(self.source()), "id", discovering=True)
        source = self.source("https://archives.example.org/music") | {
            "category": "import", "adapter": "custom", "documentation": "https://archives.example.org/docs"}
        answer = self.response(source) | {"research_id": task["subject"]}
        validate(answer, task["subject"], discovering=True)
        with patch("_parole_maintenance.research", return_value=(answer, "")):
            self.assertEqual(maint.do_codex(task)[0], "done")
        with maint.index.db() as db:
            found = db.execute("SELECT * FROM sources WHERE id='host:archives.example.org'").fetchone()
        self.assertEqual((found["category"], found["state"]), ("import", "pending"))
        controls(maint.index, found["id"], "active")
        self.assertFalse(maint.index.source_enabled(found["id"]))
        self.assertTrue(maint.queue_adapter(found))
        with maint.index.db() as db:
            tasks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM tasks WHERE kind='codex'")]
        self.assertTrue(any(data["kind"] == "adapter" and data["category"] == "import" for data in tasks))

    def test_schema_url_quota_and_discovery(self):
        source = self.source()
        self.assertEqual(validate(self.response(source), "id")["status"], "found")
        for bad in ("https://127.0.0.1/a", "http://example.org", "file:///etc/passwd",
                    "https://secret@music.org/path", "https://music.org:123/a", "https://a.local/a"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                validate(self.response(self.source(bad)), "id")
        with patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.0.1", 443))]):
            with self.assertRaises(ValueError):
                external_ips("music.example.org")
        seed(self.index)
        identity, added = discover(self.index, source, "id")
        self.assertTrue(added)
        self.assertEqual(discover(self.index, source, "x"), (identity, False))
        controls(self.index, identity, "active")
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT state FROM sources WHERE id=?", (identity,)).fetchone()[0], "pending")
        self.assertTrue(daily_slot(self.index, 1, now=1500))
        self.assertFalse(daily_slot(self.index, 1, now=1700))
        self.assertTrue(daily_slot(self.index, 1, now=90000))
        with self.assertRaises(PermissionError):
            authenticate(self.base, lambda: False, self.base / "auth.json")

    def test_disabled_musicbrainz_cannot_issue_catalogue_requests(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from _parole_work import main

        seed(self.index)
        controls(self.index, "MusicBrainz", "disabled")
        output = StringIO()
        with patch("_parole_work.Index", return_value=self.index), patch(
                "_parole_work.edition", side_effect=AssertionError("fournisseur désactivé")), patch.object(
                sys, "argv", ["_parole_work.py", "album", "fixture"]), redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue())["state"], "temporary")

    def test_validated_endpoint_is_reused_and_can_be_disabled(self):
        seed(self.index)
        source = self.source()
        identity, _ = discover(self.index, source, "id")
        engine = lyrics_engine()
        text = "[00:01.000]Enfin la musique"
        record = dict(trackName="Chanson", artistName="Groupe", albumName="Album", duration=30,
                      syncedLyrics=text)
        fixtures = [{"title": title, "artist": "Groupe", "album": "Album", "duration": 30, "synced": True,
                     "sha256": sha256(text.encode()).hexdigest() if title != "Fausse" else None}
                    for title in ("Chanson", "Autre", "Fausse")]

        def lookup(_, title, *_args, **transport):
            self.assertIs(transport["registry"], self.index)
            self.assertEqual(transport["provider"], identity)
            return [{**record, "trackName": title}] if title != "Fausse" else []

        endpoint = LyricsEndpoint("lyrics.example.org", "/api/search")
        controls(self.index, "LRCLIB", "disabled")
        controls(self.index, "NetEase", "disabled")
        controls(self.index, "QQ Music", "disabled")
        controls(self.index, "LyricDown", "disabled")
        with patch("_parole_sources.fetch", return_value=b"Conditions"), patch.object(LyricsEndpoint, "search", lookup):
            validate_endpoint(self.index, identity, endpoint, source["terms"], fixtures, engine.best_transcript)
            self.assertEqual(engine.LyricsSources().find("Chanson", "Groupe", "Album", 30, "Groupe", synced=True,
                                                         registry=self.index), (text, identity))
            controls(self.index, identity, "excluded")
            self.assertEqual(engine.LyricsSources().find("Chanson", "Groupe", "Album", 30, "Groupe", synced=True,
                                                         registry=self.index), (None, None))


@unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "Session GTK isolée requise")
class GtkTests(Fixture):
    def test_main_import_settings_details_and_tv(self):
        os.environ["KARA_SILENT"] = "1"
        os.environ["KARA_MUSIC_LIBRARY"] = str(self.root)
        loader = importlib.machinery.SourceFileLoader("gtk_parole_fixture", str(ROOT / "bin/paroles-strawberry"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app_module = importlib.util.module_from_spec(spec)
        loader.exec_module(app_module)
        from gi.repository import Gdk, Gio, GLib, Gtk
        from types import SimpleNamespace

        app_module.LIBRARY = self.root
        Gtk.init()
        app = Gtk.Application(application_id="fr.labfy.TestParoleV3", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.assertTrue(app.register(None))
        style = Gtk.CssProvider()
        style.load_from_data(app_module.CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), style,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        path = self.audio()
        with patch.object(app_module, "https_configured", return_value=False), patch.object(
                app_module, "Maintenance", side_effect=lambda root, collection, **kwargs: Maintenance(
                    root, None, state=self.base / "state", **kwargs)):
            window = app_module.LyricsWindow(app)
            window.start_maintenance(background=True)
            background = window.maintenance
            self.assertIsNone(window.parole_ui)
            self.assertFalse(background.foreground.is_set())
            window.open_parole()
        self.assertIs(window.maintenance, background)
        self.assertTrue(background.foreground.is_set())
        view = window.parole_ui
        self.addCleanup(window.destroy)
        self.addCleanup(window.close_window, window)
        window.maintenance.pause(True)
        inventory(window.maintenance.index, None, window.maintenance.inspect_cache,
                  lambda: False, lambda *_: None)
        window.maintenance.revision += 1
        view.refresh_state()
        self.assertEqual(view.cards["total"].get_label(), "1")
        self.assertEqual(view.songs.get_n_items(), 1)
        view.card_action("no_lyrics")
        self.assertEqual(view.songs.get_n_items(), 1)
        view.card_action("ready")
        self.assertEqual(view.songs.get_n_items(), 0)
        view.all()
        self.assertIsNone(window.legacy_controls.get_root())
        visible = view.songs.get_item(0)
        view.song_selection.set_selected(0)
        modifications = []
        view.songs.connect("items-changed", lambda *_: modifications.append(True))
        window.maintenance.index.update(visible.data["id"], checked=42.)
        window.maintenance.revision += 1
        view.refresh_state()
        self.assertEqual(modifications, [])
        self.assertIs(view.song_selection.get_selected_item(), visible)
        self.assertEqual(visible.data["checked"], 42.)
        window.maintenance.index.update(visible.data["id"], lyrics="Strawberry")
        window.maintenance.revision += 1
        view.refresh_state()
        self.assertEqual(modifications, [True])
        self.assertEqual(view.song_selection.get_selected_item().data["id"], visible.data["id"])
        draft = {"duration": 5., "language": "fr", "plain": "Brouillon vocal",
                 "timed": "[00:01.000]Brouillon vocal", "audio": {"sha256": "fixture"},
                 "model": {}, "settings": {}, "observation": {"input": "original", "acceleration": "vulkan"},
                 "first_pass": {"language_request": "", "words": [],
                                "segments": [{"start": .75, "end": 1.99, "no_speech": .12,
                                              "text": "Passage à écouter"}]}}
        window.maintenance.index.enqueue("transcription", visible.data["id"], "fixture")
        with patch("_parole_transcribe.read_draft", return_value=draft):
            view.details(view.songs.get_item(0).data)
        detail = next(w for w in Gtk.Window.get_toplevels() if w.get_transient_for() is window
                      and w.get_title() == visible.data["title"])

        def descendants(widget):
            yield widget
            child = widget.get_first_child()
            while child:
                yield from descendants(child)
                child = child.get_next_sibling()

        editor_buttons = {widget.get_label(): widget for widget in descendants(detail) if isinstance(widget, Gtk.Button)}
        formats = next(widget for widget in descendants(detail) if isinstance(widget, Gtk.DropDown))
        editor = next(widget for widget in descendants(detail) if isinstance(widget, Gtk.TextView))
        contents = editor.get_buffer()
        formats.set_selected(1)
        editor_buttons["Charger le brouillon Turbo"].emit("clicked")
        self.assertTrue(editor.get_editable())
        self.assertFalse(formats.get_sensitive())
        self.assertFalse(contents.get_can_undo())
        contents.insert_at_cursor(" saisie")
        self.assertTrue(contents.get_can_undo())
        contents.undo()
        self.assertEqual(contents.get_text(*contents.get_bounds(), False), draft["timed"])
        editor_buttons["Annuler la correction"].emit("clicked")
        self.assertTrue(formats.get_sensitive())
        self.assertFalse(contents.get_can_undo())
        self.assertFalse(path.with_suffix(".lrc").exists())

        expander = next(widget for widget in descendants(detail) if isinstance(widget, Gtk.Expander))
        expander.set_expanded(True)
        controls_by_label = {widget.get_label(): widget for widget in descendants(expander.get_child())
                             if isinstance(widget, Gtk.Button)}
        self.assertFalse(controls_by_label["Voix"].get_sensitive())
        with patch.object(view.draft_audition, "start") as preview:
            controls_by_label["Mix"].emit("clicked")
            self.assertEqual(preview.call_args.args[2:6], (path, visible.data["token"], .75, 1.99))
        self.assertFalse(window.permits_draft_audition())
        self.assertIn("désactivé", window.seek_draft_review(path, .75))
        with patch.dict(os.environ, {"KARA_SILENT": "0"}), patch.object(window, "player_command") as seek:
            window.track, window.player_track_id, window.duration = path, "/org/mpris/Track/fixture", 5.
            window.player_seen = app_module.time.monotonic()
            with patch.object(window, "playback_state", "Playing"):
                self.assertFalse(window.permits_draft_audition())
            self.assertIsNotNone(window.seek_draft_review(path.with_suffix(".wav"), .75))
            self.assertIsNone(window.seek_draft_review(path, .75))
            method, values = seek.call_args.args
            self.assertEqual((method, values.unpack()), ("SetPosition", (window.player_track_id, 750_000)))
        window.open_import()
        self.assertTrue(window.album_import.get_reveal_child())
        self.assertEqual(window.album_import.get_root(), window.import_window)
        imports = window.album_import
        self.assertEqual(imports.selected_provider(), "Toutes les sources")
        providers = imports.provider.get_model()
        names = [providers.get_string(i) for i in range(providers.get_n_items())]
        imports.provider.set_selected(names.index("Bandcamp"))
        self.assertEqual(imports.official_download.get_uri(), "https://bandcamp.com/")
        self.assertTrue(imports.official_download.get_visible())
        self.assertFalse(imports.artist.get_sensitive())
        imports.provider.set_selected(names.index("YouTube Music"))
        self.assertFalse(imports.official_download.get_visible())
        self.assertTrue(imports.artist.get_sensitive())
        imports.provider.set_selected(names.index("Internet Archive"))
        self.assertEqual(imports.artist.get_placeholder_text(), "Artiste ou album")
        self.assertTrue(imports.can_search_archive())
        self.assertFalse(imports.can_search_youtube())
        with patch.dict(os.environ, {"KARA_JAMENDO_CLIENT_ID": ""}):
            imports.provider.set_selected(names.index("Jamendo"))
            self.assertFalse(imports.can_search_jamendo())
            self.assertTrue(imports.official_download.get_visible())
            self.assertTrue(imports.download_license.get_visible())
        imports.provider.set_selected(names.index("Toutes les sources"))
        facts = {"title": "Album de qualification", "artist": "Atelier", "count": 1,
                 "release_id": "b90181d2-ff52-4bfb-aeb5-f8c66c8fed25",
                 "tracks": [{"title": "Piste", "disc": 1, "number": 1, "duration": 120, "isrc": "FRABC1900001"}]}
        offer = {"title": facts["title"], "artist": facts["artist"], "tracks": 1, "formats": ["FLAC"],
                 "license": "https://creativecommons.org/licenses/by/4.0/", "price": 0, "facts": facts}
        arch = offer | {"provider": "Internet Archive", "url": "https://archive.org/details/album"}
        jam = offer | {"provider": "Jamendo", "url": "https://www.jamendo.com/album/48123"}
        with patch.object(imports, "search_next_catalogue", return_value=False):
            imports.remote_results([arch], arch["provider"], "Atelier")
            imports.remote_results([jam], jam["provider"], "Atelier")
        candidate_row = imports.results.get_first_child()
        self.assertIsNone(candidate_row.get_next_sibling())
        self.assertIn("Jamendo", candidate_row.get_child().get_first_child().get_label())
        with patch.object(imports, "start_queue") as queued:
            imports.start_offer((arch["title"], arch["url"]))
        self.assertTrue(queued.call_args.args[0][0][1].startswith("parole://edition/"))
        for page in ("general", "sources", "codex", "storage", "resources", "night"):
            view.settings(page)
            self.assertEqual(view.settings_dialog.get_child().get_end_child().get_visible_child_name(), page)
        view.refresh_night_summary()
        summary = view.night_summary.get_label()
        self.assertIn("0 préparations complètes", summary)
        self.assertIn("0 références temporelles validées", summary)
        self.assertIn("Dernières 24 h révolues · Réseau 0 textes / 0 LRC · Bibliothèque 0 textes / 0 LRC", summary)
        self.assertIn("0 reports mémoire · 0 reports quota", summary)
        self.assertNotIn("Moyennes", summary)

        from time import sleep

        # Exercise the real multi-selection, preview and confirmation in a
        # mounted GTK dialog. The fixture collection alone is mutated.
        (self.root / "Lune").mkdir()
        album = self.root / "Soleil" / "Édition locale"
        album.mkdir(parents=True)
        (album / "Piste.opus").write_bytes(path.read_bytes())
        view.artist_groups(view.root)
        grouping = next(item for item in Gtk.Window.list_toplevels() if item.get_title() == "Regrouper les artistes")
        self.addCleanup(grouping.destroy)

        def widgets(widget):
            yield widget
            child = widget.get_first_child()
            while child:
                yield from widgets(child)
                child = child.get_next_sibling()

        children = list(widgets(grouping))
        selection = next(item.get_model() for item in children if isinstance(item, Gtk.ListView))
        for i in range(selection.get_n_items()):
            if selection.get_item(i).data in ("Lune", "Soleil"):
                selection.select_item(i, False)
        lookup = {item.get_label(): item for item in children if isinstance(item, Gtk.Button)}
        lookup["Prévisualiser"].emit("clicked")
        context = GLib.MainContext.default()
        for _ in range(300):
            while context.pending():
                context.iteration(False)
            if lookup["Confirmer le regroupement"].get_sensitive():
                break
            sleep(.01)
        self.assertTrue(lookup["Confirmer le regroupement"].get_sensitive())
        self.assertFalse((self.root / "Lune" / album.name).exists())
        lookup["Confirmer le regroupement"].emit("clicked")
        for _ in range(300):
            while context.pending():
                context.iteration(False)
            if any(isinstance(item, Gtk.Label) and item.get_label().startswith("Artistes regroupés") for item in children):
                break
            sleep(.01)
        self.assertTrue((self.root / "Lune" / album.name / "Piste.opus").is_file())
        window.maintenance.journal.emit("SCAN", "Fixture")
        view.console.update()
        self.assertTrue(any(e[-1] == "Fixture" for e in view.console.lines))
        view.console.hide_events(None)
        self.assertFalse(view.console.lines)
        with patch.object(window, "receiver", SimpleNamespace(https_listener=True, secure_tv_seen=__import__("time").monotonic())):
            view.update_tv()
            self.assertTrue(view.tv.has_css_class("tv-ok"))
        view.update_tv()
        self.assertTrue(view.tv.has_css_class("tv-off"))
        context = GLib.MainContext.default()
        for _ in range(30):
            if context.pending():
                context.iteration(False)


if __name__ == "__main__":
    unittest.main()
