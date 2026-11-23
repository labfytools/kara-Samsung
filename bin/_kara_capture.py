"""Enregistrement local d'un seul tour, sur demande du joueur."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
import wave


class TurnCapture:
    def __init__(self, begin, rate):
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        directory = base / "kara/diagnostic"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = NamedTemporaryFile(mode="w+b", prefix=time.strftime("tour-%Y%m%d-%H%M%S-"),
                                       suffix=".wav", dir=directory, delete=False)
        self.path = Path(self.file.name)
        self.begin = begin
        self.rate = rate
        self.frames = 0
        try:
            self.wave = wave.open(self.file, "wb")
            self.wave.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        except (OSError, wave.Error):
            self.file.close()
            self.path.unlink(missing_ok=True)
            raise

    def append(self, end, block):
        offset = round((end - self.begin) * self.rate) - len(block) // 2
        first = max(0, self.frames - offset)
        if first >= len(block) // 2:
            return
        if offset > self.frames:
            self.wave.writeframesraw(bytes((offset - self.frames) * 2))
        self.wave.writeframesraw(block[first * 2:])
        self.frames = offset + len(block) // 2

    def close(self):
        try:
            self.wave.close()
        finally:
            self.file.close()
        if self.frames == 0:
            self.path.unlink(missing_ok=True)
