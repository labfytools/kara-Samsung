
(function () {
    function element(id) { return document.getElementById(id); }
    function show(id, value) {
        var node = element(id);
        if (node.textContent !== value) node.textContent = value;
    }
    var seenReload = null;
    var seenClose = null;
    var tvIsClosed = false;
    var coverRevision = "";
    var displayedTrack;
    var displayedLine;
    var displayedCredits = "";
    var requestedScreenSaver;

    function setScreenSaver(enabled) {
        if (typeof tizen === "undefined" || requestedScreenSaver === enabled) return;
        requestedScreenSaver = enabled;
        function failed() {
            if (requestedScreenSaver !== enabled) return;
            requestedScreenSaver = undefined;
            diagnostic("screensaver-error");
        }
        try {
            var app = webapis.appcommon;
            var states = app.AppCommonScreenSaverState;
            app.setScreenSaver(enabled ? states.SCREEN_SAVER_ON : states.SCREEN_SAVER_OFF, function () {
                if (requestedScreenSaver === enabled) diagnostic(enabled ? "screensaver-on" : "screensaver-off");
            }, failed);
        } catch (error) {
            failed();
        }
    }

    function updateScreenSaver() {
        // Samsung owns the system policy. Kara holds only the foreground
        // screensaver setting, including while Strawberry is briefly paused.
        setScreenSaver(document.hidden || tvIsClosed);
    }

    function renderCredits(links) {
        var items = Array.isArray(links) ? links.slice(0, 3) : [];
        var signature = JSON.stringify(items);
        if (displayedCredits === signature) return;
        displayedCredits = signature;
        var footer = element("attribution");
        while (footer.firstChild) footer.removeChild(footer.firstChild);
        items.forEach(function (item) {
            if (!item || typeof item.label !== "string" || typeof item.url !== "string" ||
                !/^https:\/\/(lrc\.red\/s\/[A-Z]{2}[A-Z0-9]{3}[0-9]{7}|unison\.betterlyrics\.org\/(song\/[A-Za-z0-9_-]{11}|curator\/[A-Za-z0-9_-]{1,128}))$/.test(item.url)) return;
            var link = document.createElement("a");
            link.href = item.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = item.label;
            footer.appendChild(link);
        });
        footer.hidden = !footer.firstChild;
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
                updateScreenSaver();
                element("tv-closed").hidden = false;
                element("reopen-tv").focus();
            }
            seenClose = state.tv_close;
        }
        return tvIsClosed;
    }

    function render(data) {
        if (applyRemoteCommands(data)) return;
        var lines = data.lines;
        var plain = !lines.some(Boolean) ? data.plain_lyrics || "" : "";
        var cover = element("album-art");
        if (data.cover_revision !== coverRevision) {
            coverRevision = data.cover_revision || "";
            if (data.cover_data) cover.src = data.cover_data;
            else cover.removeAttribute("src");
        }
        cover.hidden = !coverRevision;
        show("title", data.title || "Kara");
        show("artist", data.artist || "");
        renderCredits(data.attribution);
        show("plain-lyrics", plain);
        element("plain-lyrics").hidden = !plain;
        show("previous", lines[0] || "");
        show("next", lines[2] || "");
        show("following", lines[3] || "");

        var text = lines[1] || (plain || lines.some(Boolean) ? "" : data.notice || "");
        // Only Strawberry advances the words. A paused or delayed poll
        // must keep the last received position, including after a seek.
        var count = lines[1] && typeof data.highlight_chars === "number" ?
            Math.max(0, Math.min(text.length, data.highlight_chars)) : 0;
        show("sung", text.slice(0, count));
        show("pending", text.slice(count));
        var track = data.track_revision === undefined ? data.title + "\n" + data.artist : data.track_revision;
        if (track !== displayedTrack || data.line !== displayedLine) {
            var forward = track === displayedTrack && data.line === displayedLine + 1 &&
                data.playback_state === "Playing";
            var lyrics = element("timed-lyrics");
            lyrics.classList.remove("advance");
            if (forward) {
                // Restart the short transition even on consecutive fast lines.
                void lyrics.offsetWidth;
                lyrics.classList.add("advance");
            }
            displayedTrack = track;
            displayedLine = data.line;
        }
        show("status", (data.recording ? "● BlackBox  " : "") +
            (data.playback_state === "Paused" ? "En pause" : ""));
    }
    var panel = element("screen");
    var connect = element("connect");
    var endpoint = window.KARA_DISPLAY_ENDPOINT || "https://kara.labfytools.com:8443/tv/etat";
    var key = window.KARA_DISPLAY_KEY;
    var timer, request, retryDelay = 750;
    var lastRendered;

    function diagnostic(event, details) {
        try {
            var xhr = new XMLHttpRequest();
            xhr.open("GET", endpoint.replace(/\/etat$/, "/diagnostic") + "?event=" + event + (details || ""), true);
            if (key) xhr.setRequestHeader("Authorization", "Bearer " + key);
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
        var began = performance.now();
        xhr.open("GET", endpoint + "?qr=0&cover=" + encodeURIComponent(coverRevision), true);
        xhr.timeout = 2600;
        if (key) xhr.setRequestHeader("Authorization", "Bearer " + key);
        xhr.onloadend = function () {
            if (request !== xhr) return;
            request = null;
            if (xhr.status !== 200) { unavailable(); return; }
            try {
                var state = JSON.parse(xhr.responseText);
                if (!state || !Array.isArray(state.lines)) { unavailable(); return; }
                render(state);
                if (state.recording && state.line_revision !== undefined && state.line_revision !== lastRendered) {
                    lastRendered = state.line_revision;
                    var painted = performance.now();
                    diagnostic("rendered", "&revision=" + state.line_revision + "&browser_ms=" +
                        painted.toFixed(1) + "&rtt_ms=" + (painted - began).toFixed(1));
                }
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
        updateScreenSaver();
        clearTimeout(timer);
        if (request) { var previous = request; request = null; previous.abort(); }
        retryDelay = 750;
        show("hint", "Connexion au serveur Kara…");
        poll();
    }
    element("retry").onclick = retry;
    element("reopen-tv").onclick = function () { window.location.reload(); };
    document.addEventListener("visibilitychange", updateScreenSaver);
    window.addEventListener("pageshow", updateScreenSaver);
    window.addEventListener("pagehide", function () { setScreenSaver(true); });
    document.addEventListener("keydown", function (event) {
        if (event.keyCode === 10009 || event.keyCode === 27) {
            event.preventDefault();
            if (typeof tizen !== "undefined") {
                setScreenSaver(true);
                tizen.application.getCurrentApplication().exit();
            }
        }
    });
    element("retry").focus();
    retry();
}());
