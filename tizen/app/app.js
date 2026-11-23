
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
            return {name: player.name, song: player.song || "", score: player.score,
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
            var displayScore = active || player.done;
            var color = playerColor(player.name);
            if (card.title.textContent !== (player.song || "")) card.title.textContent = player.song || "";
            card.points.textContent = displayScore ? score + " %" : (player.connected ? "●" : "");
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
        if (data.qr_data && !element("room-qr").getAttribute("data-inline-qr")) {
            element("room-qr").setAttribute("data-inline-qr", "1");
            element("room-qr").src = "data:image/png;base64," + data.qr_data;
        }
        var lines = data.lines || ["", "", "", ""];
        var progress = Math.max(0, Math.min(1, data.progress || 0));
        var current = element("current");
        var text = lines[1] || data.notice || " ";
        var count = Math.floor(progress * text.length);
        show("title", data.title || "Karaoké");
        show("artist", data.artist || "");
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
        show("status", data.status || "");
        element("fill").style.width = (100 * progress) + "%";
    }
    var form = element("pair");
    var panel = element("screen");
    var pc = element("pc");
    var code = element("code");
    var hint = element("hint");
    var endpoint = "";
    var connection = 0;

    function showRoomQRCode() {
        var base = endpoint.replace(/\/etat$/, "");
        if (!base || base === lastRoomURL) return;
        lastRoomURL = base;
        element("room-qr").src = base + "/qr";
        element("room-url").textContent = base + "/jouer";
    }

    function destination(value) {
        var match = /^(?:http:\/\/)?(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(value.trim());
        if (!match) return "";
        var ip = match.slice(1).map(Number);
        if (ip.some(function (x) { return x > 255; }) ||
                !(ip[0] === 10 || ip[0] === 192 && ip[1] === 168 ||
                  ip[0] === 172 && ip[1] >= 16 && ip[1] <= 31)) return "";
        return "http://" + ip.join(".") + ":8765";
    }

    function remember() {
        try {
            localStorage.setItem("kara.pc", pc.value);
            localStorage.setItem("kara.code", code.value);
        } catch (error) {}
    }

    function disconnect() {
        connection += 1;
        endpoint = "";
        lastRoomURL = "";
        form.hidden = false;
        panel.hidden = true;
        hint.textContent = "";
        pc.focus();
    }

    function poll(id) {
        if (id !== connection || !endpoint) return;
        var xhr = new XMLHttpRequest();
        xhr.open("GET", endpoint, true);
        xhr.timeout = 1900;
        xhr.onloadend = function () {
            if (id !== connection) return;
            if (xhr.status === 200) {
                try {
                    var state = JSON.parse(xhr.responseText);
                    if (!form.hidden) remember();
                    form.hidden = true;
                    panel.hidden = false;
                    showRoomQRCode();
                    render(state);
                } catch (error) {
                    hint.textContent = "Réponse du PC illisible.";
                }
            } else if (xhr.status === 404) {
                disconnect();
                hint.textContent = "Vérifie le code affiché dans Paroles Strawberry.";
                return;
            } else {
                var message = "En attente du mode TV sur le PC…";
                hint.textContent = message;
                if (!panel.hidden) show("status", message);
            }
            setTimeout(function () { poll(id); }, xhr.status === 200 ? 160 : 1150);
        };
        xhr.send();
    }

    form.onsubmit = function (event) {
        event.preventDefault();
        var host = destination(pc.value);
        var token = code.value.trim().toLowerCase();
        if (!host || !/^[0-9a-f]{8}$/.test(token)) {
            hint.textContent = "Saisis l’adresse du PC et le code à huit caractères.";
            return;
        }
        hint.textContent = "Connexion au PC…";
        endpoint = host + "/" + token + "/etat";
        connection += 1;
        poll(connection);
    };

    element("reopen-tv").onclick = function () { window.location.reload(); };

    document.addEventListener("keydown", function (event) {
        if (event.keyCode === 10009 || event.keyCode === 27) {
            event.preventDefault();
            if (endpoint) disconnect();
            else if (typeof tizen !== "undefined") tizen.application.getCurrentApplication().exit();
            return;
        }
        if (!form.hidden && (event.keyCode === 38 || event.keyCode === 40)) {
            var controls = [pc, code, element("join")];
            var direction = event.keyCode === 40 ? 1 : -1;
            var index = controls.indexOf(document.activeElement);
            controls[(index + direction + controls.length) % controls.length].focus();
            event.preventDefault();
        }
    });

    try {
        pc.value = localStorage.getItem("kara.pc") || "192.168.1.63";
        code.value = localStorage.getItem("kara.code") || "";
    } catch (error) {}
    if (code.value) form.onsubmit({preventDefault:function () {}});
    else code.focus();
}());
