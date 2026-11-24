#!/usr/bin/env python3
"""Pilote les consignes visuelles d'une future séance Kara sur Android."""
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from _kara_paths import music_library
from _kara_prepare import PreparedCache

TAKES = (
    ("Parlé", "Sortie physique coupée. Dis les mots en suivant le repère visuel."),
    ("Chanté", "Sortie physique coupée. Chante les mêmes mots dans le tempo."),
    ("Chanté faux", "Sortie physique coupée. Change volontairement la hauteur, garde les mots et le tempo."),
    ("Chanté en retard", "Sortie physique coupée. Commence environ 1,5 seconde après le repère visuel."),
    ("Avec accompagnement", "Instrumental audible au niveau choisi. Chante les mêmes mots dans le tempo."),
    ("Joueur silencieux", "Même instrumental, même niveau. Reste silencieux pendant tout le tour."),
)

PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kara · Consignes du tour</title>
<style>
body{background:#14171d;color:#f5f5f5;font:1.15rem system-ui;margin:2rem auto;padding:0 1rem;max-width:36rem}
#take{color:#9bd8ce}#lyrics{white-space:pre-line;line-height:1.6}
</style>
<main><h1 id="take">Préparation</h1><p id="instruction"></p><p id="lyrics"></p></main>
<script>
const targets = ['take', 'instruction', 'lyrics'];
async function poll() {
    try {
        const response = await fetch('/cue', {cache:'no-store'});
        const cue = await response.json();
        targets.forEach(key => { document.getElementById(key).textContent = cue[key]; });
    } catch (_) {
        document.getElementById('take').textContent = 'Connexion interrompue';
    }
    setTimeout(poll, 600);
}
poll();
</script>
</html>""".encode()


def serve_cues(adb, lyrics):
    cue = ("Préparation", "Attends le signal de Codex.", lyrics)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def do_GET(self):
            if self.path == "/":
                content_type, payload = "text/html; charset=utf-8", PAGE
            elif self.path == "/cue":
                content_type = "application/json; charset=utf-8"
                payload = json.dumps(dict(zip(("take", "instruction", "lyrics"), cue)), ensure_ascii=False).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Permissions-Policy", "microphone=(), camera=(), autoplay=()")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"tcp:{server.server_port}"
        mapped = False
        try:
            # Use a private loopback port and remove only our own mapping.
            # The phone is a cue screen. Live capture is an explicit GTK action.
            adb("reverse", "--no-rebind", endpoint, endpoint)
            mapped = True
            adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                "-d", f"http://127.0.0.1:{server.server_port}/")
            for number, (title, instruction) in enumerate(TAKES, 1):
                cue = (f"{number}/{len(TAKES)} · {title}", instruction, lyrics)
                print(f"\n{cue[0]} · {instruction}", flush=True)
                input("Entrée après la prise et son contrôle dans Kara… ")
            cue = ("Séance terminée", "Les diagnostics restent sur le PC.", lyrics)
        finally:
            server.shutdown()
            thread.join()
            if mapped:
                adb("reverse", "--remove", endpoint)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="Identifiant explicite renvoyé par adb devices -l")
    parser.add_argument("--song", type=Path, required=True)
    parser.add_argument("--first", type=int, required=True, help="Première ligne préparée (base 1)")
    parser.add_argument("--last", type=int, required=True, help="Dernière ligne préparée (incluse)")
    parser.add_argument("--dry-run", action="store_true", help="Vérifie les consignes et la référence sans appeler ADB")
    args = parser.parse_args()
    if not (args.dry_run or args.serial):
        parser.error("Indique le numéro de série avec --serial.")
    song = (music_library() / args.song).resolve(strict=True)
    reference = PreparedCache().cached(song)
    if reference is None or not 1 <= args.first <= args.last <= len(reference["lines"]):
        parser.error("Morceau préparé et intervalle de lignes valide requis.")
    lines = reference["lines"][args.first - 1:args.last]
    if len(lines) > 4 or any(not any(word["start"] is not None for word in line["words"]) for line in lines):
        parser.error("Sélectionne une à quatre lignes possédant des repères évaluables.")
    lyrics = "\n".join(line["text"] for line in lines)
    print(f"{reference['title']} · {lines[0]['display_start']:.2f} s · {len(lines)} ligne(s)", flush=True)
    if args.dry_run:
        for number, (title, instruction) in enumerate(TAKES, 1):
            print(f"{number}. {title} · {instruction}")
        return

    def adb(*arguments):
        return subprocess.run(["adb", "-s", args.serial, *arguments], check=True, timeout=10)

    adb("get-state")
    try:
        serve_cues(adb, lyrics)
    except (EOFError, KeyboardInterrupt):
        print("\nConsignes arrêtées.", file=sys.stderr)


if __name__ == "__main__":
    main()
