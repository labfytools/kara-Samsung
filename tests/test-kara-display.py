#!/usr/bin/python3 -B
"""Contrat réel du serveur de lecture seule sur le réseau local."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_tv import Receiver


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.journal = Mock()
        self.receiver = Receiver("127.0.0.1", diagnostics=self.journal)
        self.addCleanup(self.receiver.close)

    def get(self, segment="", *, method="GET"):
        with urlopen(Request(self.receiver.url + segment, method=method), timeout=2) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.read()

    def test_lrc_state_and_local_renderer(self):
        state = dict(title="Morceau", lines=["", "Mot suivant"], highlight_chars=3,
                     line=0, line_revision=1, position=1.2, playback_state="Paused")
        self.receiver.update(state, cover=("pochette", "data:image/png;base64,AA=="))
        self.assertIn(b'id="sung"', self.get())
        self.assertNotIn(b"Bearer", self.get("display-config.js"))
        first = json.loads(self.get("etat?cover="))
        self.assertEqual(first["cover_data"], "data:image/png;base64,AA==")
        self.assertEqual({key: first[key] for key in state}, state)
        self.assertNotIn("cover_data", json.loads(self.get("etat?cover=pochette")))
        self.get("diagnostic?event=rendered&revision=1")
        self.assertEqual(self.journal.emit.call_args.kwargs["position"], 1.2)
        self.receiver.request_tv_close()
        self.assertEqual(json.loads(self.get("etat"))["tv_close"], 1)
        self.receiver.resume_tv_display()
        self.assertEqual(json.loads(self.get("etat"))["tv_reload"], 1)

    def test_removed_clients_and_protected_samsung_route(self):
        for endpoint, method in (("jouer", "GET"), ("api/etat", "GET"), ("qr", "GET"),
                                 ("mobile.js", "GET"), ("api/rejoindre", "POST")):
            with self.assertRaises(HTTPError) as error:
                self.get(endpoint, method=method)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen("http://" + self.receiver.host + "/tv/etat", timeout=2)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_screensaver_report_uses_the_authenticated_samsung_route(self):
        with self.assertRaises(HTTPError) as error:
            self.get("diagnostic?event=screensaver-off")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with patch("builtins.print") as journal:
            self.receiver.tv_report({"event": ["screensaver-off"]}, secure_display=True)
        journal.assert_called_once_with("Kara TV screensaver-off", flush=True)
        self.assertEqual(self.journal.emit.call_args.args, ("television", "screensaver-off"))


if __name__ == "__main__":
    unittest.main()
