"""Mobile commands run on the existing Parole controller's owning main loop."""
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import Future, TimeoutError
import re
from threading import BoundedSemaphore
import time
from uuid import uuid4

from gi.repository import GLib

from _kara_import_queue import digest
from _kara_mobile_room import RoomError
from _kara_party import ORIGINAL_VOICE_LEVELS


class RemoteParty:
    def __init__(self, controller):
        self.control = controller
        self.inflight = BoundedSemaphore(8)
        self.receipts = OrderedDict()
        self.history = defaultdict(list)
        self.phase = "lobby"
        self.identity = uuid4().hex
        self.queue = deque()
        self.plan = []
        self.current = None
        self.next_at = 0.
        self.notice = ""
        self.turn_id = None
        self.revision = 0
        self.opened_early = False
        self.deferred = []
        self.eligible = set()
        self.completed_paths = defaultdict(set)
        self.unscored_turns = set()
        self.retries = set()
        self.background_path = None
        self.defaults = {"music": 100, "original": 0, "voice": None}
        self.last_result = None

    @property
    def locked(self):
        return self.phase in ("preparing", "running")

    def dispatch(self, action, name, data):
        # A timed-out command remains idempotent if already running. A command
        # that expired in the queue is cancelled before it can affect playback.
        if not self.inflight.acquire(blocking=False):
            raise RoomError("Parole est occupé. Réessaie.", 503)
        future = Future()

        def execute():
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(self.command(action, name, data))
                    except Exception as error:
                        future.set_exception(error)
            finally:
                self.inflight.release()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(execute)
        try:
            return future.result(timeout=2.3)
        except TimeoutError as error:
            future.cancel()
            raise RoomError("Parole répond lentement. Vérifie l’état avant de réessayer.", 503) from error
        except (OSError, ValueError) as error:
            if isinstance(error, RoomError):
                raise
            raise RoomError(str(error), 409) from error
        except Exception as error:
            self.control.blackbox.emit("party", "error", operation=action)
            raise RoomError("La commande a échoué dans Parole.", 500) from error

    def command(self, action, name, data):
        control = self.control
        if control.closed or control.receiver is None:
            raise RoomError("La salle a fermé.", 409)
        key = data.get("request")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9-]{24,40}", key):
            raise RoomError("Identifiant de commande invalide.")
        receipt = (name, key)
        if receipt in self.receipts:
            old_action, result = self.receipts[receipt]
            if old_action != action:
                raise RoomError("Ce numéro de commande a déjà été utilisé.", 409)
            return result
        if action in ("start", "open", "next", "end", "new", "pause", "resume", "prepare", "skip-failed", "sing-only",
                      "replay", "defer-player", "return-player", "allow-unscored", "player-order", "remove-player"):
            if type(data.get("revision")) is not int or data["revision"] != self.revision:
                raise RoomError("L’état de la partie a changé. Actualise la commande.", 409)

        if action == "blackbox-start":
            options = {"tracks": data["tracks"]} if "tracks" in data else {}
            control.start_blackbox(data.get("consent"), voice=data.get("voice") is True, **options)
        elif action == "blackbox-stop":
            control.stop_blackbox()
        elif action == "prepare":
            self.prepare(data.get("retry") is True)
        elif action == "open" and self.phase in ("lobby", "ready"):
            turns = self.selected_turns()
            if not turns or not self.can_offer(*turns[0]):
                raise RoomError("Le premier morceau doit être prêt pour ouvrir le salon.", 409)
            self.opened_early = True
            self.plan = turns
            self.queue = deque(turns)
            self.eligible = {token for token, (_, admin, songs) in control.receiver.room.selections().items()
                             if not admin or songs}
            self.phase = "running"
            self.next_at = time.monotonic()
        elif action in ("player-start", "organizer-start"):
            player = control.party.active
            if (not self.opened_early or not player or action == "player-start" and player.name != name
                    or data.get("turn") != self.turn_id):
                raise RoomError("Le démarrage est réservé au joueur du tour et à l’organisateur.", 403)
            control.request_turn_start()
        elif action == "player-retry-import":
            token = control.receiver.room.player_token(name)
            if token is None:
                raise RoomError("Joueur introuvable.", 404)
            control.mobile_imports.action(data.get("job"), "retry", owner=digest(token))
        elif action in ("player-retry-song", "retry-song", "allow-unscored"):
            self.manage_song(action, name if action == "player-retry-song" else data.get("player"),
                             data.get("song"))
        elif action == "skip-failed" and self.phase == "lobby" and control.can_skip_failed():
            control.skip_failed(None)
            self.phase = "ready"
        elif action == "sing-only" and self.phase == "lobby" and control.unscored_choices():
            control.allow_unscored(None)
            self.phase = "ready"
        elif action == "start" and self.phase == "ready" and control.party_ready:
            self.queue = deque((player, song) for player, song in self.plan
                               if song.path not in control.party.excluded)
            if not self.queue:
                raise RoomError("Aucun tour prêt.", 409)
            self.phase = "running"
            self.next_at = time.monotonic()
            self.tick(self.next_at)
        elif action in ("pause", "resume") and self.phase == "running" and control.turn_phase == "playing":
            control.player_command("Pause" if action == "pause" else "Play")
        elif action in ("next", "end") and self.phase == "running":
            if data.get("confirmed") is not True:
                raise RoomError("Confirme l’interruption du morceau ou de la partie.")
            if action == "end":
                self.queue.clear()
                self.deferred.clear()
            if control.party.active is not None:
                control.finish_turn(pause=True, error="Tour interrompu par l’organisateur.")
            elif action == "next" and self.queue:
                self.deferred.append(self.queue.popleft())
            if not self.queue and not self.deferred:
                self.finish()
        elif action == "replay" and self.phase in ("running", "finished"):
            player_name = data.get("player")
            turn = (self.current if self.current and self.current[0].name == player_name else
                    next((item for item in reversed(self.plan) if item[0].name == player_name
                          and item[1].title == data.get("song")), None))
            if not turn or data.get("confirmed") is not True:
                raise RoomError("Choisis un tour à rejouer et confirme.")
            if self.current == turn:
                control.finish_turn(pause=True, error="Tour à rejouer.")
            self.queue.appendleft(turn)
            self.phase = "running"
        elif action == "defer-player" and self.phase == "running":
            pending = [turn for turn in self.queue if turn[0].name == data.get("player")]
            active = (self.current if self.current and self.current[0].name == data.get("player")
                      and control.turn_phase in ("preparing", "awaiting-mic") else None)
            if active:
                pending.insert(0, active)
            if not pending:
                raise RoomError("Aucun tour en attente pour ce joueur.", 409)
            self.queue = deque(turn for turn in self.queue if turn not in pending)
            self.deferred.extend(pending)
            if active:
                control.finish_turn(error="Tour reporté.")
        elif action == "return-player" and self.phase == "running":
            pending = [turn for turn in self.deferred if turn[0].name == data.get("player")]
            if not pending:
                raise RoomError("Aucun tour reporté pour ce joueur.", 409)
            self.deferred = [turn for turn in self.deferred if turn not in pending]
            self.queue.extend(pending)
        elif action == "new" and self.phase in ("finished", "ready", "lobby") and not control.party.active:
            self.reset()
        elif action == "player-order" and self.phase in ("lobby", "ready", "running"):
            self.reorder_players(data.get("players"))
        elif action == "remove-player" and self.phase in ("lobby", "ready", "running"):
            player = next((p for p in control.party.players if p.name == data.get("player")), None)
            if player is None or player is control.party.active or player.name == name:
                raise RoomError("Ce joueur ne peut pas être retiré.", 409)
            if player.mobile_id:
                control.receiver.room.kick(player.mobile_id)
                self.eligible.discard(player.mobile_id)
            else:
                control.party.remove(player)
            self.queue = deque(turn for turn in self.queue if turn[0] is not player)
            self.deferred = [turn for turn in self.deferred if turn[0] is not player]
            self.plan = [turn for turn in self.plan if turn[0] is not player]
        elif action == "audio-defaults":
            values = {key: data.get(key) for key in self.defaults}
            if any(type(value) is not int or not 0 <= value <= 100 for value in values.values()):
                raise RoomError("Les trois volumes doivent être compris entre 0 et 100.")
            self.defaults = values
        elif action == "audio-reset-player":
            self.set_player_mix(data.get("player"), {key: value for key, value in self.defaults.items()
                                                     if value is not None})
            if self.defaults["voice"] is None:
                owner = control.receiver.room.player_token(data.get("player"))
                control.room_audio.reset_player_volume(owner)
        elif action == "audio-return-cut":
            if control.party.active is None or control.party.active.name != name:
                raise RoomError("Ce retour vocal est réservé au joueur du tour.", 403)
            control.room_audio.mute_return()
        elif action == "music-volume":
            control.set_remote_music_volume(data.get("percent"))
        elif action in ("audio-profile", "audio-output", "audio-ceiling", "audio-cut"):
            control.room_audio.administrate(action, data.get("value"))
        elif action == "audio-player":
            player = data.get("player")
            owner = control.receiver.room.player_token(player)
            if owner is None:
                raise RoomError("Joueur introuvable.", 404)
            control.room_audio.set_player_volume(owner, player, data.get("percent"))
        elif action in ("audio-player-mix", "audio-personal"):
            self.set_player_mix(name if action == "audio-personal" else data.get("player"), data)
        elif action in ("import-pause", "import-resume", "import-concurrency"):
            options = ({"batches": data.get("batches")} if action == "import-concurrency"
                       else {"paused": action == "import-pause"})
            if action == "import-concurrency" and type(options["batches"]) is not int:
                raise RoomError("Nombre de lots invalide.")
            if not control.album_import.configure(**options):
                raise RoomError("Impossible de modifier les réglages de la file.", 409)
        elif action in ("import-approve", "import-retry", "import-cancel"):
            control.mobile_imports.action(data.get("job"), action.split("-")[1])
        else:
            raise RoomError("Cette commande est indisponible dans l’état actuel.", 409)

        if action not in ("blackbox-start", "blackbox-stop", "music-volume") and not action.startswith(("import-", "audio-")):
            self.revision += 1
        control.blackbox.emit("party", "command", player=name, action=action, turn=self.turn_id)
        self.publish()
        result = {"ok": True, "revision": self.revision}
        self.receipts[receipt] = (action, result)
        if len(self.receipts) > 128:
            self.receipts.popitem(last=False)
        return result

    def changed_selection(self):
        if self.phase == "ready":
            self.phase = "lobby"
        if self.phase == "lobby":
            self.revision += 1
            self.notice = ""

    def selected_turns(self):
        selections = self.control.receiver.room.selections()
        choices = []
        for player in self.control.party.players:
            if not player.mobile_id:
                if player.song:
                    choices.append((player, [player.song]))
                continue
            ready, _, songs = selections.get(player.mobile_id, (False, False, []))
            if ready and songs and all(songs):
                choices.append((player, songs))
        return [(player, songs[index]) for index in range(max((len(songs) for _, songs in choices), default=0))
                for player, songs in choices if index < len(songs)]

    def can_offer(self, player, song):
        return self.control.song_available(song, unscored=self.unscored_for(player, song))

    def unscored_for(self, player, song):
        return player.training or (player, song.path) in self.unscored_turns

    def waiting_players(self):
        room = self.control.receiver.room
        return {token for token, (ready, _, _) in room.selections().items() if token in self.eligible and not ready}

    def editable_players(self):
        if not self.opened_early or self.phase != "running":
            return set()
        people = {player.mobile_id for player, _ in (*self.queue, *self.deferred)} | self.waiting_players()
        if self.current:
            people.discard(self.current[0].mobile_id)
        return people

    def revised_selection(self, player):
        if not self.opened_early or self.phase != "running" or player.mobile_id not in self.eligible:
            return
        if self.current and player is self.current[0]:
            return
        ready, _, songs = self.control.receiver.room.selections().get(player.mobile_id, (False, False, []))
        upcoming = [(player, song) for song in songs if ready and song and
                    song.path not in self.completed_paths[player.mobile_id]]
        container = self.deferred if any(item[0] is player for item in self.deferred) else self.queue
        index = next((i for i, item in enumerate(container) if item[0] is player), len(container))
        replacement = [item for item in container if item[0] is not player]
        replacement[index:index] = upcoming
        if container is self.queue:
            self.queue = deque(replacement)
        else:
            self.deferred = replacement
        for turn in upcoming:
            if turn not in self.plan:
                self.plan.append(turn)
        self.revision += 1

    def manage_song(self, action, name, title):
        turns = self.plan if self.opened_early else self.selected_turns()
        selected = next(((player, song) for player, song in turns if player.name == name and song.title == title), None)
        if selected is None or selected[1].path not in self.control.failed_alignments:
            raise RoomError("Aucune préparation en échec pour ce morceau.", 409)
        player, match = selected
        if action == "allow-unscored":
            if not self.control.song_available(match, unscored=True):
                raise RoomError("Les pistes du morceau sont indisponibles.", 409)
            self.unscored_turns.add((player, match.path))
        else:
            self.retries.add(match.path)

    def background(self, candidates):
        control = self.control
        if control.validating or control.party.active is not None or control.game_score.requested:
            return
        for _, song in candidates:
            force = song.path in self.retries
            if not force and (song.path in control.failed_alignments or control.song_available(song)):
                continue
            self.background_path = song.path
            if control.begin_preparation([song], for_party=False, force=force):
                self.retries.discard(song.path)
            else:
                self.background_path = None
            break

    def set_player_mix(self, name, data):
        control = self.control
        player = next((p for p in control.party.players if p.name == name and p.mobile_id), None)
        if player is None or control.receiver.room.player_token(name) != player.mobile_id:
            raise RoomError("Joueur introuvable.", 404)
        for key in ("music", "voice"):
            if key in data and (type(data[key]) is not int or not 0 <= data[key] <= 100):
                raise RoomError("Le volume doit être compris entre 0 et 100.")
        training = data.get("training", player.training)
        original = data.get("original", player.original_voice)
        if type(training) is not bool or type(original) is not int or not 0 <= original <= 100:
            raise RoomError("Réglage de voix originale invalide.")
        if training != player.training and self.phase not in ("lobby", "ready", "finished"):
            raise RoomError("Le mode de notation se choisit avant de lancer la partie.", 409)
        if "voice" in data:
            control.room_audio.set_player_volume(player.mobile_id, player.name, data["voice"])
        music = data.get("music", player.music_volume)
        music_changed = music != player.music_volume
        mode_changed = training != player.training
        original_changed = original != player.original_voice
        player.music_volume = music
        player.training, player.original_voice = training, original
        if mode_changed and self.phase in ("lobby", "ready"):
            control.party_ready = False
            self.changed_selection()
        if music_changed or mode_changed or original_changed:
            turn = self.turn_id if player is control.party.active else None
            control.blackbox.emit("audio", "player-mix", player=player.name, turn=turn, music=music,
                                   training=training, original=original)
            if player is control.party.active:
                control.apply_player_mix(player, music_changed=music_changed, original_changed=original_changed)

    def training_paths(self):
        training = {song.path for player, song in self.plan if player.training}
        scored = {song.path for player, song in self.plan if not player.training}
        return training, training - scored

    def reorder_players(self, names):
        players = {player.name: player for player in self.control.party.players}
        if (not isinstance(names, list) or len(names) != len(players)
                or any(not isinstance(name, str) for name in names) or set(names) != set(players)):
            raise RoomError("L’ordre des joueurs est invalide.")
        self.control.party.players[:] = [players[name] for name in names]
        if self.phase == "running":
            ranks = {name: index for index, name in enumerate(names)}
            # Preserve the selected song order for each player.
            self.queue = deque(sorted(self.queue, key=lambda turn: ranks[turn[0].name]))
        else:
            self.control.party_ready = False
        if self.control.players_window:
            self.control.players_window.refresh()

    def prepare(self, retry=False):
        control = self.control
        if self.phase not in ("lobby", "ready") or control.validating or control.party.active:
            raise RoomError("Une préparation ou un tour est en cours.", 409)
        if control.album_import.running or control.mobile_imports.pending():
            raise RoomError("Attends la fin des demandes d’importation.", 409)
        selections = control.receiver.room.selections()
        choices = []
        for player in control.party.players:
            if player.mobile_id is None:
                if player.song is not None:
                    choices.append((player, [player.song]))
                continue
            if player.mobile_id not in selections:
                continue
            ready, organizer, songs = selections[player.mobile_id]
            if organizer and not songs:
                continue
            if not ready or not songs or any(song is None for song in songs):
                raise RoomError(f"{player.name} doit confirmer sa sélection.", 409)
            player.song = songs[0]
            choices.append((player, songs))
        if not choices:
            raise RoomError("Choisis au moins une chanson avant la préparation.", 409)
        self.plan = [(player, songs[index]) for index in range(max(len(songs) for _, songs in choices))
                     for player, songs in choices if index < len(songs)]
        control.ensure_players_window()
        self.phase = "preparing"
        self.notice = ""
        control.receiver.room.set_mode(locked=True, game_mode=True, current="")
        options = {"force_paths": self.control.failed_alignments.keys()} if retry else {}
        if not control.begin_preparation([song for _, song in self.plan], for_party=True, **options):
            self.phase = "lobby"
            raise RoomError("La préparation n’a pas démarré.", 409)

    def prepared(self):
        if self.phase == "preparing":
            self.phase = "ready" if self.control.party_ready else "lobby"
            self.revision += 1

    def tick(self, now):
        if self.phase in ("lobby", "ready") and self.control.receiver is not None:
            self.background(self.selected_turns())
        if (self.phase != "running" or self.control.party.active is not None or self.current is not None
                or now < self.next_at):
            return
        if not self.queue:
            if not self.deferred and (not self.opened_early or not self.waiting_players()):
                self.finish()
            return
        if self.opened_early:
            player, song = self.queue[0]
            if not self.can_offer(player, song):
                self.background(self.queue)
                return
            if player.mobile_id and not self.control.receiver.room.is_online(player.mobile_id):
                self.notice = f"En attente de {player.name}."
                return
            if self.control.validating:
                if self.background_path is not None:
                    self.control.interrupt_preload()
                return
            if player.mobile_id and not self.control.receiver.room.reserve_turn(player.mobile_id, song):
                # A choice changed between heartbeat and handoff. Reconcile on the
                # owning loop and keep the newly selected song in the same position.
                if not self.control.receiver.room.player_token(player.name):
                    self.queue.popleft()
                else:
                    self.revised_selection(player)
                return
            self.control.ensure_players_window()
            self.notice = ""
        player, song = self.queue.popleft()
        self.current = player, song
        self.turn_id = uuid4().hex[:16]
        self.revision += 1
        player.song = song
        if self.opened_early:
            self.control.start_turn(player, independent=True)
        else:
            self.control.start_turn(player)
        if self.control.party.active is None:
            if self.opened_early and song.path in self.control.failed_alignments:
                self.queue.appendleft(self.current)
                self.current = self.turn_id = None
                return
            self.turn_completed(player, "Tour indisponible.", played=False)
            if not self.opened_early and not self.control.party_ready:
                self.queue.clear()
                self.finish()

    def turn_completed(self, player, detail, *, played):
        if not self.current or player is not self.current[0]:
            return
        song = self.current[1]
        result = {"title": song.title, "artist": song.artist, "played": played,
                  "summary": (player.result or {}).get("summary") if played else None, "detail": detail}
        if played or self.current not in self.deferred:
            self.history[player.name].append(result)
            self.last_result = {"player": player.name, **result}
            if player.mobile_id:
                self.completed_paths[player.mobile_id].add(song.path)
        self.notice = detail
        self.current = None
        self.turn_id = None
        self.revision += 1
        self.next_at = time.monotonic() + 3.

    def latest_result(self):
        return self.last_result if self.current is None and time.monotonic() < self.next_at else None

    def finish(self):
        if self.phase == "finished":
            return
        self.phase = "finished"
        self.revision += 1
        self.control.blackbox.emit("party", "party-end", turns=sum(len(rows) for rows in self.history.values()))
        self.control.stop_blackbox("party-end")

    def reset(self):
        self.phase = "lobby"
        self.identity = uuid4().hex
        self.history.clear()
        self.plan.clear()
        self.queue.clear()
        self.deferred.clear()
        self.eligible.clear()
        self.completed_paths.clear()
        self.unscored_turns.clear()
        self.opened_early = False
        self.retries.clear()
        self.last_result = None
        self.notice = ""
        self.control.party_ready = False
        self.control.party.excluded.clear()
        self.control.party.unscored.clear()
        self.control.receiver.room.reset_ready()
        selections = self.control.receiver.room.selections()
        for player in self.control.party.players:
            player.score, player.done, player.result = None, False, None
            if player.mobile_id in selections:
                player.song = next(iter(selections[player.mobile_id][2]), None)
        if self.control.players_window:
            self.control.players_window.refresh()

    def ranking(self):
        ranking = []
        for name, rows in self.history.items():
            notes = [row["summary"]["score"] for row in rows if row["summary"] and row["summary"]["score"] is not None]
            ranking.append({"name": name, "score": round(sum(notes) / len(notes)) if notes else None,
                            "scored": len(notes), "turns": len(rows)})
        ranking.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["name"].casefold()))
        return ranking

    def publish(self):
        control = self.control
        receiver = control.receiver
        if receiver is None:
            return
        active = control.party.active
        blackbox = control.blackbox.status(storage=True)
        if preflight := getattr(control, "blackbox_preflight", None):
            blackbox["preflight"] = preflight()
        music_limit = control.turn_music_restore if control.turn_music_restore is not None else control.music_volume
        audio = {**control.room_audio.status(),
                 "music_ceiling": round(100 * min(1., music_limit)) if music_limit is not None else 100}
        mixes = {p.name: {"music": p.music_volume, "voice": control.room_audio.preferred_volume(p.mobile_id),
                          "training": p.training, "original": p.original_voice}
                 for p in control.party.players if p.mobile_id}
        unscored = defaultdict(list)
        for player, song in self.plan or self.selected_turns():
            if self.unscored_for(player, song) and self.can_offer(player, song):
                unscored[player.name].append(song.title)
        receiver.room.publish({
            "public": {"phase": self.phase, "revision": self.revision, "turn": self.turn_id,
                       "turn_phase": control.turn_phase, "player": active.name if active else None,
                       "countdown": control.countdown() if self.opened_early else 0,
                       "mic_receiving": control.room_audio.receiving(recent=True) if self.opened_early else False,
                       "score": (control.game_score.breakdown or {}).get("score") if active and self.opened_early else None,
                       "song": active.song.title if active and active.song else None,
                       "next": [{"player": player.name, "song": song.title} for player, song in self.queue],
                       "progress": control.align_fraction, "notice": control.align_status if control.validating else self.notice,
                       "tv": time.monotonic() - (receiver.secure_tv_seen if receiver.display_key else receiver.tv_seen) < 4,
                       "recording": blackbox["active"], "recording_voice": blackbox["voice"],
                       "last_result": self.last_result,
                       "ranking": self.ranking(), "audio": audio, "player_start": self.opened_early,
                       "original_voice_levels": ORIGINAL_VOICE_LEVELS},
            "admin": {"blackbox": blackbox, "failed": control.align_status if control.failed_alignments else "",
                      "can_skip": control.can_skip_failed(), "can_sing_only": bool(control.unscored_choices()),
                      "paused": control.playback_state == "Paused", "defaults": self.defaults,
                      "can_open": self.can_open(),
                      "deferred": [p.name for p, _ in self.deferred],
                      "players": [{"name": player.name, "mobile": bool(player.mobile_id),
                                   "mix": mixes.get(player.name)} for player in control.party.players]},
            "mix": mixes,
            "unscored": unscored,
            "results": {name: list(rows) for name, rows in self.history.items()},
        })

    def can_open(self):
        if self.phase not in ("lobby", "ready"):
            return False
        turns = self.selected_turns()
        return bool(turns and self.can_offer(*turns[0]))
