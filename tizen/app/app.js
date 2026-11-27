
(function () {
    function element(id) { return document.getElementById(id); }
    function show(id, value) {
        var node = element(id);
        if (node.textContent !== value) node.textContent = value;
    }
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

    function renderPlayers(players, connected) {
        var list = element("players");
        var records = players.length ? players : connected;
        var roster = records.map(function (entry) {
            return {name: entry.name, song: entry.song || "", active: !!entry.active};
        });
        var signature = JSON.stringify(roster);
        show("player-count", String(roster.length));
        element("stage").classList.toggle("with-players", roster.length > 0);
        if (list.getAttribute("data-roster") === signature) return;
        list.setAttribute("data-roster", signature);
        list.textContent = "";
        roster.forEach(function (entry) {
            var item = document.createElement("li");
            if (entry.active) item.className = "singing";
            item.style.setProperty("--player-color", playerColor(entry.name));
            var name = document.createElement("strong");
            name.className = "player-name";
            name.textContent = entry.name;
            var song = document.createElement("small");
            song.className = "player-song";
            song.textContent = entry.song;
            item.appendChild(name);
            item.appendChild(song);
            list.appendChild(item);
        });
    }

    var coverRevision = "";

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
        var music = data.display_mode === "music" || !data.display_mode && data.party_phase !== "running";
        var playing = data.turn_phase === "playing";
        var showLyrics = music || playing;
        var lines = showLyrics ? data.lines || [] : [];
        var notice = data.notice || " ";
        var plain = showLyrics && Array.isArray(lines) && !lines.some(Boolean) ? data.plain_lyrics || "" : "";
        var cover = element("album-art");
        if (data.cover_revision !== coverRevision) {
            coverRevision = data.cover_revision || "";
            if (data.cover_data) cover.src = data.cover_data;
            else cover.removeAttribute("src");
        }
        cover.hidden = !music || !coverRevision;
        if (!music) {
            if (data.countdown > 0) notice = String(data.countdown);
            else if (data.recent_result) notice = data.recent_result.player + " · " + data.recent_result.detail;
            else if (!playing) notice = data.current_player ? "À toi de chanter, " + data.current_player + " !" : "Salon ouvert";
        }
        element("mobile-room").hidden = !music;
        element("room-qr").hidden = !element("room-qr").getAttribute("data-inline-qr");
        element("organizer-code").hidden = !data.organizer_pin;
        show("pin", data.organizer_pin || "");
        element("progress").hidden = !showLyrics || !lines[1];
        var progress = showLyrics ? Math.max(0, Math.min(1, data.progress || 0)) : 0;
        var current = element("current");
        var text = lines[1] || (plain ? "" : notice);
        var count = lines[1] && typeof data.highlight_chars === "number" ? data.highlight_chars : Math.floor(progress * text.length);
        show("title", (music ? data.title : data.current_song || (playing && data.title)) || "Kara");
        show("artist", showLyrics ? data.artist || "" : "");
        show("plain-lyrics", plain);
        element("plain-lyrics").hidden = !plain;
        var players = Array.isArray(data.players) ? data.players : [];
        var connected = Array.isArray(data.connected) ? data.connected : [];
        renderPlayers(players, connected);
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
        show("status", (data.recording ? "● BlackBox   " : "") +
            (music && data.playback_state === "Paused" ? "En pause" : ""));
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
        xhr.open("GET", endpoint + "?qr=" + (qr.getAttribute("data-inline-qr") ? "0" : "1") +
            "&cover=" + encodeURIComponent(coverRevision), true);
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
