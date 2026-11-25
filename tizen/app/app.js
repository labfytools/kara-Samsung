
(function () {
    function element(id) { return document.getElementById(id); }
    function show(id, value) {
        var node = element(id);
        if (node.textContent !== value) node.textContent = value;
    }
    var playerNames = [];
    var playerRows = [];
    var playerColors = Object.create(null);
    var nextColor = 0;
    var colors = ["#f38ba8", "#fab387", "#f9e2af", "#a6e3a1", "#94e2d5",
                  "#89b4fa", "#b4befe", "#cba6f7", "#f5c2e7", "#74c7ec",
                  "#eba0ac", "#a6adc8", "#f2cdcd", "#c6a0f6", "#a3e635",
                  "#fb7185", "#2dd4bf", "#fdba74", "#818cf8", "#fde047"];
    var seenReload = null;
    var seenClose = null;
    var tvIsClosed = false;
    var lastRoomURL = "";
    var lastUpcoming = "";


    function playerColor(name) {
        if (!Object.prototype.hasOwnProperty.call(playerColors, name)) {
            var occupied = Object.keys(playerColors).map(function (key) { return playerColors[key]; });
            var free = colors.filter(function (color) { return occupied.indexOf(color) < 0; });
            playerColors[name] = free.length ? free[0] : "hsl(" + ((nextColor * 137.5) % 360) + ", 75%, 72%)";
            nextColor += 1;
        }
        return playerColors[name];
    }

    function applyRemoteCommands(state) {
        if (typeof state.tv_reload === "number") {
            if (seenReload !== null && seenReload !== state.tv_reload) {
                window.location.reload();
                return true;
            }
            seenReload = state.tv_reload;
        }
        if (typeof state.tv_close === "number") {
            if (seenClose !== null && seenClose !== state.tv_close) {
                tvIsClosed = true;
                element("tv-closed").hidden = false;
                element("reopen-tv").focus();
            }
            seenClose = state.tv_close;
        }
        return tvIsClosed;
    }

    function scoreColor(score) {
        var colors = [[243, 139, 168], [249, 226, 175], [166, 227, 161]];
        var step = score < 50 ? 0 : 1;
        var fraction = (score - 50 * step) / 50;
        return "rgb(" + colors[step].map(function (value, index) {
            return Math.round(value + (colors[step + 1][index] - value) * fraction);
        }).join(",") + ")";
    }

    function addPlayerRow(name) {
        var row = document.createElement("li");
        row.style.setProperty("--player-color", playerColor(name));
        var identity = document.createElement("div");
        var heading = document.createElement("strong");
        var title = document.createElement("span");
        var points = document.createElement("strong");
        var gauge = document.createElement("div");
        var fill = document.createElement("div");
        identity.className = "player-identity";
        heading.className = "player-name";
        heading.textContent = name;
        title.className = "player-song";
        points.className = "player-score";
        gauge.className = "player-gauge";
        gauge.setAttribute("aria-hidden", "true");
        fill.className = "player-fill";
        identity.appendChild(heading);
        identity.appendChild(title);
        gauge.appendChild(fill);
        row.appendChild(identity);
        row.appendChild(points);
        row.appendChild(gauge);
        element("players").appendChild(row);
        return {row: row, title: title, points: points, fill: fill, gauge: gauge};
    }

    function renderPlayers(players, connected) {
        // Les joueurs du téléphone restent visibles sur la TV même hors jeu.
        // Un joueur présent à la fois dans la partie et la salle n'est listé
        // qu'une seule fois. Les lignes sont colorées par identité.
        var online = connected.map(function (player) { return player.name; });
        var merged = players.filter(function (player) {
            // N'afficher les joueurs mobiles que s'ils sont connectés, sauf
            // le chanteur déjà en cours de tour (sa partie doit rester visible).
            return !player.mobile || player.active || online.indexOf(player.name) >= 0;
        }).map(function (player) {
            return {name: player.name, song: player.song || "", score: player.score, coverage: player.coverage,
                    done: !!player.done, active: !!player.active, connected: false};
        });
        connected.forEach(function (mobile) {
            var found = null;
            merged.some(function (player) {
                if (player.name === mobile.name) { found = player; return true; }
                return false;
            });
            if (found) {
                found.connected = true;
                if (!found.song) found.song = mobile.song || "";
            } else {
                merged.push({name: mobile.name, song: mobile.song || "", score: null,
                             done: false, active: false, connected: true});
            }
        });
        var list = element("players");
        var names = merged.map(function (player) { return player.name; });
        Object.keys(playerColors).forEach(function (name) {
            if (names.indexOf(name) < 0) delete playerColors[name];
        });
        show("player-count", String(connected.length));
        if (JSON.stringify(names) !== JSON.stringify(playerNames)) {
            playerNames = names;
            list.textContent = "";
            playerRows = names.map(addPlayerRow);
        }
        merged.forEach(function (player, index) {
            var card = playerRows[index];
            var active = !!player.active;
            var score = Math.max(0, Math.min(100, Number(player.score) || 0));
            var displayScore = (active || player.done) && player.score != null;
            var color = playerColor(player.name);
            if (card.title.textContent !== (player.song || "")) card.title.textContent = player.song || "";
            card.points.textContent = displayScore ? score + " %" : (player.done ? "—" : (player.connected ? "●" : ""));
            if (player.done && player.coverage != null) {
                card.points.textContent += " · " + player.coverage + " % évalués";
            }
            card.points.style.color = color;
            card.fill.style.backgroundColor = color;
            card.fill.style.width = displayScore ? score + "%" : "0%";
            card.gauge.hidden = !displayScore;
            card.row.classList.toggle("singing", active);
            if (active) {
                card.row.setAttribute("aria-current", "true");
                var top = card.row.offsetTop;
                if (top < list.scrollTop || top + card.row.offsetHeight > list.scrollTop + list.clientHeight) {
                    list.scrollTop = top;
                }
            } else {
                card.row.removeAttribute("aria-current");
            }
        });
    }

    function render(data) {
        if (applyRemoteCommands(data)) return;
        if (data.join_url !== lastRoomURL) {
            element("room-qr").removeAttribute("data-inline-qr");
            lastRoomURL = data.join_url;
        }
        show("room-url", data.join_url || "");
        if (data.qr_data && !element("room-qr").getAttribute("data-inline-qr")) {
            element("room-qr").setAttribute("data-inline-qr", "1");
            element("room-qr").src = "data:image/png;base64," + data.qr_data;
        }
        var lobby = !data.party_phase || ["lobby", "preparing", "ready"].indexOf(data.party_phase) >= 0;
        var recent = data.recent_result;
        var playing = data.turn_phase === "playing";
        var lines = playing ? data.lines || [] : [];
        var notice = data.notice || " ";
        if (lobby) notice = "En attente de l’ouverture du salon";
        else if (data.party_phase === "finished") notice = "Partie terminée";
        else if (data.countdown > 0) notice = String(data.countdown);
        else if (recent) notice = recent.player + " · " + (recent.summary && recent.summary.score != null ?
            recent.summary.score + " %" : recent.detail);
        else if (!playing) notice = data.current_player ? "À toi de chanter, " + data.current_player + " !" : "Salon ouvert";
        element("mobile-room").hidden = !lobby;
        element("room-qr").hidden = !element("room-qr").getAttribute("data-inline-qr");
        element("organizer-code").hidden = !data.organizer_pin;
        show("pin", data.organizer_pin || "");
        element("progress").hidden = !playing;
        var progress = playing ? Math.max(0, Math.min(1, data.progress || 0)) : 0;
        var current = element("current");
        var text = lines[1] || notice;
        var count = Math.floor(progress * text.length);
        show("title", lobby ? "Kara" : data.current_song || (playing && data.title) || "Kara");
        show("artist", playing ? data.artist || "" : "");
        var players = Array.isArray(data.players) ? data.players : [];
        var connected = Array.isArray(data.connected) ? data.connected : [];
        renderPlayers(players, connected);
        show("score", players.length || data.score == null ? "" : data.score + " %");
        show("previous", lines[0] || "");
        if (!current.firstChild || current.getAttribute("data-text") !== text ||
                current.getAttribute("data-progress") !== String(count)) {
            current.textContent = "";
            current.setAttribute("data-text", text);
            current.setAttribute("data-progress", String(count));
            var sung = document.createElement("span");
            sung.className = "sung";
            sung.textContent = text.slice(0, count);
            current.appendChild(sung);
            current.appendChild(document.createTextNode(text.slice(count)));
        }
        show("next", lines[2] || "");
        show("following", lines[3] || "");
        var ranking = data.ranking || [];
        var standings = ranking.map(function (item) {
            return item.name + " · " + (item.score == null ? "sans note" : item.score + " %");
        }).join("   |   ");
        show("status", (data.recording ? "● BlackBox   " : "") +
            (data.party_phase === "finished" ? "Classement · " + standings : lobby && data.preparing_count ?
                data.preparing_count + " morceau(x) en préparation" : playing ? data.status || "" : ""));
        var upcoming = data.party_phase === "running" && !data.current_player ? data.next_turns || [] : [];
        var order = JSON.stringify(upcoming);
        if (lastUpcoming !== order) {
            lastUpcoming = order;
            var list = element("upcoming");
            list.textContent = "";
            upcoming.forEach(function (turn) {
                var item = document.createElement("li");
                item.textContent = turn.player + " · " + turn.song;
                list.appendChild(item);
            });
        }
        element("fill").style.width = (100 * progress) + "%";
    }
    var panel = element("screen");
    var connect = element("connect");
    var hint = element("hint");
    var endpoint = "https://kara.labfytools.com:8443/tv/etat";
    var key = window.KARA_DISPLAY_KEY;
    var timer, request, retryDelay = 750;
    var lastRendered;

    function diagnostic(event, details) {
        if (!key) return;
        try {
            var xhr = new XMLHttpRequest();
            xhr.open("GET", endpoint.replace(/\/etat$/, "/diagnostic") + "?event=" + event + (details || ""), true);
            xhr.setRequestHeader("Authorization", "Bearer " + key);
            xhr.timeout = 1400;
            xhr.send();
        } catch (error) {}
    }

    function unavailable() {
        var moveFocus = connect.hidden;
        connect.hidden = false;
        panel.hidden = true;
        show("hint", "Serveur Kara indisponible — nouvelle tentative en cours");
        if (moveFocus) element("retry").focus();
        timer = setTimeout(poll, retryDelay);
        retryDelay = Math.min(12000, retryDelay * 2);
    }

    function poll() {
        var xhr = request = new XMLHttpRequest();
        var qr = element("room-qr");
        var began = performance.now();
        xhr.open("GET", endpoint + "?qr=" + (qr.getAttribute("data-inline-qr") ? "0" : "1"), true);
        xhr.timeout = 2600;
        if (key) xhr.setRequestHeader("Authorization", "Bearer " + key);
        xhr.onloadend = function () {
            if (request !== xhr) return;
            request = null;
            if (xhr.status !== 200) { unavailable(); return; }
            try {
                var state = JSON.parse(xhr.responseText);
                if (!state.join_url) { unavailable(); return; }
                render(state);
                if (state.recording && state.line_revision !== undefined && state.line_revision !== lastRendered) {
                    lastRendered = state.line_revision;
                    var painted = performance.now();
                    diagnostic("rendered", "&revision=" + state.line_revision + "&browser_ms=" +
                        painted.toFixed(1) + "&rtt_ms=" + (painted - began).toFixed(1));
                }
                if (!qr.getAttribute("data-inline-qr")) lastRoomURL = "";
                panel.hidden = false;
                connect.hidden = true;
                retryDelay = 750;
                timer = setTimeout(poll, 160);
            } catch (error) { diagnostic("render-error"); unavailable(); }
        };
        try { xhr.send(); }
        catch (error) { request = null; unavailable(); }
    }

    function retry() {
        clearTimeout(timer);
        if (request) { var previous = request; request = null; previous.abort(); }
        retryDelay = 750;
        show("hint", "Connexion au serveur Kara…");
        poll();
    }
    element("retry").onclick = retry;
    element("reopen-tv").onclick = function () { window.location.reload(); };
    document.addEventListener("keydown", function (event) {
        if (event.keyCode === 10009 || event.keyCode === 27) {
            event.preventDefault();
            if (typeof tizen !== "undefined") tizen.application.getCurrentApplication().exit();
        }
    });
    element("retry").focus();
    retry();
}());
