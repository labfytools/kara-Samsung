"""Chemins partagés par Parole et ses commandes, même via un lanceur symbolique."""
import os
from pathlib import Path
import re
import sys

BIN = Path(__file__).resolve().parent


def xdg(name, fallback):
    directory = Path(os.environ.get(name, ""))
    return directory if directory.is_absolute() else Path.home() / fallback


DATA = xdg("XDG_DATA_HOME", ".local/share")
STATE = xdg("XDG_STATE_HOME", ".local/state")
CONFIG = xdg("XDG_CONFIG_HOME", ".config")
CACHE = xdg("XDG_CACHE_HOME", ".cache")


def music_library():
    configured = os.environ.get("KARA_MUSIC_LIBRARY")
    if configured:
        # A relative override must never select a different library as cwd changes.
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            raise ValueError(f"KARA_MUSIC_LIBRARY doit être un chemin absolu : {configured}")
        return directory.resolve()
    try:
        settings = (CONFIG / "user-dirs.dirs").read_text(encoding="utf-8")
    except FileNotFoundError:
        settings = ""
    match = re.search(r'^XDG_MUSIC_DIR="((?:[^"\\]|\\.)*)"\s*$', settings, re.MULTILINE)
    if match:
        value = match[1].removeprefix("$HOME/") if match[1].startswith("$HOME/") else match[1]
        directory = Path(re.sub(r'\\(["`$\\])', r'\1', value))
        if match[1].startswith("$HOME/"):
            directory = Path.home() / directory
        # XDG uses HOME itself when a special user directory is disabled.
        if directory.is_absolute() and directory != Path.home():
            return directory.resolve()
    return (Path.home() / "Music").resolve()


def kara_data(relative):
    local = DATA / "kara" / relative
    bundled = BIN.parent / ".runtime/kara-data" / relative
    return local if local.exists() or not bundled.exists() else bundled


def catalogue_command(*arguments):
    configured = os.environ.get("KARA_CATALOGUE_PYTHON")
    if configured:
        python = Path(configured).expanduser()
        if not python.is_absolute():
            raise ValueError(f"KARA_CATALOGUE_PYTHON doit être un chemin absolu : {configured}")
    else:
        venv = kara_data("catalogue/bin/python")
        python = venv if venv.is_file() else Path(sys.executable)
    # Keep the chosen venv entry point even when its executable is a symlink.
    return [str(python), "-B", str(BIN / "_kara_catalogue.py"), *arguments]
