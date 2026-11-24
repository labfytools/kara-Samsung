"""Publie un calage Turbo fiable dans le .lrc original, avec sauvegarde.

Le contenu textuel et les métadonnées non temporelles sont préservés.
Les anciens LRC sont conservés séparément hors de la bibliothèque.
"""
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
from math import isfinite
from tempfile import NamedTemporaryFile
import time

from _kara_paths import STATE
from _kara_timing import line_signature
from _paroles_lrc import MAX_LRC_BYTES, OFFSET, STAMP, WORD_STAMP, parse_lrc

BACKUPS = STATE / 'kara/lrc-backups'
MIN_COVERAGE = .55
MIN_REFINED_FRACTION = .65


def _timecode(seconds):
    cents = int(round(seconds * 100))
    minutes, rest = divmod(cents, 6000)
    secs, decimal = divmod(rest, 100)
    return f'[{minutes:02d}:{secs:02d}.{decimal:02d}]'


def _has_realignment(old_rows, stamps):
    return any(abs(float(stamp) - old) >= .02 for (old, _), stamp in zip(old_rows, stamps))


def _confidence(event, rows):
    """Ne pas écraser un minutage correct sur simple interpolation hasardeuse."""
    coverage = float(event.get('coverage') or 0)
    marked = event.get('word_marks') or []
    words_lines = sum(bool(text.strip()) for _, text in rows)
    reliable = sum(bool(p) for p in marked)
    refined = int(event.get('refined') or 0)
    if not words_lines:
        return False
    return coverage >= MIN_COVERAGE or (
        refined >= 4 and refined / words_lines >= MIN_REFINED_FRACTION
        and reliable / words_lines >= MIN_REFINED_FRACTION
    )


def publish(song, event, expected_signature, *, backup_root=BACKUPS):
    """Écrit seulement un vrai recalage suffisamment sûr.

    Retourne (statut, rows, backup_path), statut parmi
    'updated', 'unchanged', 'unavailable', 'insufficient'.
    Ne crée jamais une sauvegarde si le fichier est inchangé.
    """
    song = Path(song).resolve(strict=True)
    lrc = song.with_suffix('.lrc')
    if lrc.is_symlink() or not lrc.is_file():
        raise ValueError('LRC absent ou lien symbolique : remplacement refusé')
    if event.get('signature') != expected_signature:
        raise ValueError('Les repères ne correspondent pas au LRC analysé')
    stamps = event.get('times')
    if stamps is None:
        return 'unavailable', None, None
    if not isinstance(stamps, (tuple, list)):
        raise ValueError('Liste de minutage invalide')
    try:
        original_bytes = lrc.read_bytes()
        if len(original_bytes) > MAX_LRC_BYTES:
            raise ValueError('LRC trop grand')
        original = original_bytes.decode('utf-8-sig')
    except UnicodeError as err:
        raise ValueError('LRC non UTF-8') from err
    rows = parse_lrc(original)
    if line_signature(rows) != expected_signature or len(rows) != len(stamps):
        raise ValueError('LRC modifié depuis la préparation : écriture refusée')
    if not all(isinstance(t,(int,float)) and isfinite(t) and 0 <= t <= 36000 for t in stamps):
        raise ValueError('Minutage hors limites')
    if not all(a + .011 < b for a,b in zip(stamps,stamps[1:])):
        raise ValueError('Minutage non strictement croissant')
    if not _has_realignment(rows, stamps):
        return 'unchanged', rows, None
    if not _confidence(event, rows):
        return 'insufficient', rows, None

    # Ne pas convertir silencieusement un format de karaoké déjà enrichi.
    if WORD_STAMP.search(original):
        raise ValueError('LRC déjà minuté par mot : conversion automatique refusée')
    newline = '\r\n' if '\r\n' in original else '\n'
    other_lines = []
    for source_line in original.splitlines():
        if not STAMP.match(source_line.lstrip('\ufeff \t')):
            other_lines.append(source_line)
    offset = OFFSET.search(original)
    offset_seconds = int(offset[1]) / 1000 if offset else 0.
    # Préserver [offset:] et écrire les nouveaux temps en tenant compte de ce décalage.
    if any(t + offset_seconds < 0 for t in stamps):
        raise ValueError('Offset négatif incompatible avec le nouveau minutage')
    out = []
    for when, (_, phrase) in zip(stamps, rows):
        for subline in phrase.split('\n'):
            out.append(f'{_timecode(when + offset_seconds)} {subline}'.rstrip())
    metadata = list(other_lines)
    while metadata and not metadata[-1].strip():
        metadata.pop()
    result = newline.join([*metadata, *out]) + newline
    encoded = result.encode('utf-8')
    if len(encoded) > MAX_LRC_BYTES:
        raise ValueError('Le nouveau LRC dépasserait la taille autorisée')
    parsed = parse_lrc(result)
    if len(parsed) != len(rows) or any(
            newtext != text or abs(newt - target) > .011
            for (newt,newtext),(_,text),target in zip(parsed,rows,stamps)):
        raise ValueError('La reconstruction du LRC ne conserve pas toutes les paroles')

    # Vérifier à nouveau juste avant commit afin de ne pas effacer une édition humaine.
    if lrc.read_bytes() != original_bytes:
        raise ValueError('Le LRC a changé pendant le calage')
    source_stat = lrc.stat()
    token = sha256(os.fsencode(lrc.resolve())).hexdigest()[:20]
    history = backup_root / token
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = history / f'{datetime.now().strftime("%Y%m%d-%H%M%S")}-{time.time_ns()}.lrc'
    backup.write_bytes(original_bytes)
    os.chmod(backup, 0o600)
    temp = None
    try:
        with NamedTemporaryFile('wb', dir=lrc.parent, prefix=f'.{lrc.name}.',
                                suffix='.tmp', delete=False) as output:
            temp = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, source_stat.st_mode & 0o777)
        # Compare signatures and source stat to detect concurrent edits.
        newer = lrc.stat()
        if newer.st_ino != source_stat.st_ino or newer.st_mtime_ns != source_stat.st_mtime_ns or newer.st_size != source_stat.st_size:
            raise ValueError('Le LRC a été modifié entre lecture et enregistrement')
        os.replace(temp, lrc)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return 'updated', parsed, backup
