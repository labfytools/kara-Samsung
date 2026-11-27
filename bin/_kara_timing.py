"""Réglages de minutage par morceau pour Paroles Strawberry."""
import json
from math import isfinite
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class LyricTiming:
    def __init__(self, path):
        self.path = path
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            entries = {}
        self.entries = entries if isinstance(entries, dict) else {}

    def entry(self, song):
        item = self.entries.get(str(song), {})
        return item if isinstance(item, dict) else {}

    def offset(self, song):
        value = self.entry(song).get("offset", 0.0)
        return float(value) if isinstance(value, (float, int)) and isfinite(value) else 0.0

    def set_offset(self, song, offset):
        # Keep legacy keys in the persisted entry for archived versions.
        self.save(song, {**self.entry(song), "offset": round(offset, 2)})

    def save(self, song, entry):
        updated = {**self.entries, str(song): entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as draft:
                temp = Path(draft.name)
                json.dump(updated, draft, ensure_ascii=False, indent=2)
                draft.write("\n")
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.entries = updated
