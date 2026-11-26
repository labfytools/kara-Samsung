#!/usr/bin/python3 -B
"""Compare the optional native reader with FFprobe on complete media fixtures."""
import argparse
from base64 import b64encode
from collections import Counter
import json
from pathlib import Path
import resource
import sqlite3
from struct import pack
import subprocess
import sys
from time import monotonic, time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _parole_index import Index, inventory, probe_track
from _parole_taglib import build, read

FORMATS = {"opus": "libopus", "flac": "flac", "mp3": "libmp3lame", "ogg": "libvorbis", "m4a": "aac"}
CREDIT = {"title": "Piste témoin", "artist": "Artiste, Invité", "album_artist": "Artiste",
          "album": "Édition témoin", "track": "3/9", "disc": "2/2", "lyrics": "[00:00.23]Texte\n[00:01.10]Témoin",
          "ISRC": "ZZABC2600001", "MusicBrainz Recording Id": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
          "MusicBrainz Album Id": "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"}


def command(*arguments):
    subprocess.run(("ffmpeg", "-nostdin", "-y", "-v", "error", *map(str, arguments)), check=True)


def make_media(directory, image, *, revised):
    picture = image.read_bytes()
    mime = b"image/png"
    vorbis_picture = b64encode(pack(">II", 3, len(mime)) + mime + pack(">IIIIII", 0, 32, 32, 24, 0, len(picture))
                               + picture).decode("ascii")
    files = {}
    for extension, codec in FORMATS.items():
        target = directory / f"{'revised' if revised else 'original'}.{extension}"
        options = ["-f", "lavfi", "-i", "sine=f=440:r=48000:d=1.37"]
        if extension in ("flac", "mp3", "m4a"):
            options += ["-i", image, "-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
        options += ["-c:a", codec]
        metadata = dict(CREDIT, title="Piste retaguée" if revised else CREDIT["title"])
        if extension in ("opus", "ogg"):
            metadata["METADATA_BLOCK_PICTURE"] = vorbis_picture
        for name, value in metadata.items():
            options.extend(("-metadata", f"{name}={value}"))
        command(*options, target)
        files[extension] = target
    return files


def comparable(tags):
    return {key: tags.get(key) for key in ("title", "artist", "album", "albumartist", "lyrics",
                                           "isrc", "musicbrainzrecordingid", "musicbrainzalbumid")} | {
        "track": tags.get("track") or tags.get("tracknumber"),
        "disc": tags.get("disc") or tags.get("discnumber"),
    }


def status(directory, helper):
    for extension in FORMATS:
        path = directory / f"original.{extension}"
        ff, pictures = probe_track(path)
        fast = read(path, helper)
        if not fast:
            raise SystemExit(f"Le témoin nominal {extension} est tombé en repli")
        expected, observed = comparable(ff), comparable(fast.tags)
        differences = {key: [expected[key], observed[key]] for key in expected if expected[key] != observed[key]}
        print(json.dumps({"format": extension, "fast": True, "difference": differences,
                          "artwork": [bool(pictures), fast.artwork],
                          "durations": [ff["duration_seconds"], fast.tags["duration_seconds"]]}))
        if differences or not (pictures and fast.artwork) or abs(
                ff["duration_seconds"] - fast.tags["duration_seconds"]) > .15:
            raise SystemExit(f"Régression des métadonnées du témoin {extension}")
    keyed = directory / "keyed.m4a"
    command("-i", directory / "original.m4a", "-map", "0:a", "-c", "copy", "-movflags", "use_metadata_tags",
            "-metadata", "MusicBrainz Recording Id=aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa", keyed)
    if read(keyed, helper) is not None or not probe_track(keyed)[0].get("musicbrainzrecordingid"):
        raise SystemExit("Le repli a perdu les métadonnées M4A indexées")
    print(json.dumps({"format": "m4a keyed metadata", "fast": False, "identifier_by_ffprobe": True}))


def retag(base, files, revised, helper, amount, *, reference):
    from os import environ

    root = base / "music"
    root.mkdir(parents=True)
    index = Index(root, base / "state")
    original = {extension: probe_track(path) for extension, path in files.items()}
    raw = {extension: file.read_bytes() for extension, file in files.items()}
    replacement = {extension: file.read_bytes() for extension, file in revised.items()}
    paths = []
    for number in range(amount):
        edition = root / f"Édition-{number//10:05}"
        edition.mkdir(exist_ok=True)
        extension = tuple(FORMATS)[number % len(FORMATS)]
        path = edition / f"piste-{number:05}.{extension}"
        path.write_bytes(raw[extension])
        paths.append(path)
    inspect = lambda *_: ("", "absent", {})
    inventory(index, None, inspect, lambda: False, lambda *_: None, paths=paths,
              reader=lambda path: original[path.suffix[1:]])
    for path in paths:
        path.write_bytes(replacement[path.suffix[1:]])
    index.notify(paths, now=time()-5)
    environ["KARA_TAGLIB_READER"] = str(helper)
    counts = Counter()
    phases = Counter()
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    before = monotonic()
    while events := index.events(time())[1]:
        result = inventory(index, None, inspect, lambda: False, lambda *_: None, events=events,
                           reader=probe_track if reference else None)
        counts.update({key: result[key] for key in ("changed", "probes", "taglib", "transactions", "delivered")})
        phases.update(result["phase_seconds"])
    seconds = monotonic()-before
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    with index.db() as db:
        assert db.execute("SELECT count(*) FROM songs WHERE title='Piste retaguée'").fetchone()[0] == amount
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    print(json.dumps(dict(reference=reference, seconds=seconds, phases=phases, **counts,
                          child_cpu=child.ru_stime+child.ru_utime-children.ru_stime-children.ru_utime)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--mode", choices=("qualify", "reference", "fast"), default="qualify")
    parser.add_argument("--tracks", type=int, default=1000)
    args = parser.parse_args()
    folder = args.workspace.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    helper = folder / "parole-tags-v1"
    if not helper.exists():
        build(helper)
    image = folder / "cover.png"
    if not image.exists():
        command("-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", 1, image)
    files = make_media(folder, image, revised=False)
    if args.mode == "qualify":
        status(folder, helper)
        return
    revised = make_media(folder, image, revised=True)
    retag(folder / args.mode, files, revised, helper, args.tracks, reference=args.mode == "reference")


if __name__ == "__main__":
    main()
