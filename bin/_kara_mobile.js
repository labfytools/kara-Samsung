(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const root = location.pathname.replace(/\/jouer\/?$/, "");
  const storageKey = "kara.session." + root;
  const labels = {lobby: "Choix des chansons", preparing: "Préparation des morceaux", ready: "La partie est prête",
    running: "Partie en cours", finished: "Partie terminée"};
  const catalogueActions = new Set(["artistes-externes", "albums-artiste", "edition-externe"]);
  let token = localStorage.getItem(storageKey) || "";
  let state, activeView, stateTimer, searchTimer, generation = 0, offset = 0, album = null, pollRunning = false;
  let mic = null, micGeneration = 0, micMessage = "", lastInvitation;
  let resultCount = 0;
  let externalTimer, externalRevision = 0, artistBusy = false, pendingArtistSearch, wantedTrack = "";
  const coverUrls = new Map();
  let noticeTimer;
  let renderKey = "";
  let myMixKey = "", adminPlayersKey = "";
  let diagnosticAt = 0;
  function diagnostic(kind) {
    if (!token || !activeView || !state?.party.recording || performance.now() - diagnosticAt < 700) return;
    diagnosticAt = performance.now();
    api("diagnostic", {kind, view: activeView}).catch(() => {});
  }
  function notice(message) {
    $("notices").textContent = message || "";
    clearTimeout(noticeTimer);
    if (message) noticeTimer = setTimeout(() => { $("notices").textContent = ""; }, 7000);
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function button(text, callable, css) {
    const item = element("button", text, css);
    item.type = "button";
    item.onclick = () => run(item, callable);
    return item;
  }
  async function run(item, callable) {
    if (item.disabled) return;
    item.disabled = true;
    try { await callable(); } catch (error) { notice(error.message); }
    finally { item.disabled = false; }
  }
  async function api(action, data, query) {
    const headers = token ? {Authorization: "Bearer " + token} : {};
    if (data !== undefined) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), catalogueActions.has(action) ? 45000 : 5500);
    try {
      const response = await fetch(root + "/api/" + action + (query ? "?" + query : ""), {
        method: data === undefined ? "GET" : "POST", headers,
        body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store", signal: controller.signal
      });
      const payload = await response.json();
      if (!response.ok) throw Object.assign(new Error(payload.error || "Le serveur ne répond pas."), {status: response.status});
      return payload;
    } finally { clearTimeout(timer); }
  }
  async function command(action, data = {}, endpoint = "admin/" + action) {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
    const request = {request: id, revision: state.party.revision, ...data};
    try { await api(endpoint, request); }
    catch (error) {
      if (error.status) throw error;
      await api(endpoint, request);
    }
    await poll();
  }
  function remember(value) {
    token = value || "";
    if (token) localStorage.setItem(storageKey, token);
    else localStorage.removeItem(storageKey);
  }
  function show(view) {
    activeView = view;
    for (const item of $("nav").querySelectorAll("button[data-view]")) {
      const selected = item.dataset.view === view;
      $("view-" + item.dataset.view).hidden = !selected;
      item.setAttribute("aria-current", String(selected));
    }
    render();
    diagnostic("screen");
    if (view === "library") search(true);
  }
  function connected() {
    $("join").hidden = true;
    $("room").hidden = $("nav").hidden = false;
    if (!activeView) show("party");
  }
  function disconnected() {
    stopMicrophone();
    remember("");
    state = null;
    activeView = null;
    myMixKey = adminPlayersKey = "";
    $("join").hidden = false;
    $("room").hidden = $("nav").hidden = true;
    $("identity").textContent = "La soirée";
  }
  async function poll() {
    if (!token || pollRunning) return;
    pollRunning = true;
    clearTimeout(stateTimer);
    try {
      state = await api("etat");
      $("connection").textContent = "Connecté";
      $("connection").classList.add("online");
      connected();
      const party = state.party;
      const turn = party.player === state.me.name && ["awaiting-mic", "opening", "playing"].includes(party.turn_phase);
      if (!turn || !party.audio?.available) stopMicrophone("room-ended");
      if (turn) keepAwake();
      if (turn && party.turn !== lastInvitation && party.audio?.available) {
        lastInvitation = party.turn;
        micMessage = "";
        show("mic");
      }
      const results = party.results?.length || 0;
      if (!turn && results > resultCount && activeView === "mic") show("results");
      resultCount = results;
      render();
    } catch (error) {
      $("connection").textContent = "Reconnexion…";
      $("connection").classList.remove("online");
      if (error.status === 401) { disconnected(); notice("Ta session a expiré. Rejoins la soirée."); }
    } finally {
      pollRunning = false;
      if (token) stateTimer = setTimeout(poll, 1100);
    }
  }
  let wakeLock = null, nextWakeAttempt = 0;
  async function keepAwake() {
    if (!navigator.wakeLock || !token || document.hidden || wakeLock || performance.now() < nextWakeAttempt) return;
    nextWakeAttempt = performance.now() + 15000;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } catch (_) {}
  }
  $("join-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      const member = await api("inscription", {name: $("nickname").value});
      remember(member.token);
      await poll();
      keepAwake();
    });
  };
  $("leave").onclick = () => run($("leave"), async () => {
    if (!confirm("Quitter la soirée et supprimer ta sélection ?")) return;
    await api("quitter", {});
    disconnected();
  });
  $("mark").onclick = () => run($("mark"), async () => {
    await api("signaler", {});
    notice("Le moment a été marqué dans BlackBox.");
  });
  for (const item of $("nav").querySelectorAll("button")) item.onclick = () => show(item.dataset.view);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && mic) {
      stopMicrophone("background");
      micMessage = "Micro interrompu en arrière-plan. Réactive-le pour reprendre.";
    } else if (!document.hidden) { nextWakeAttempt = 0; poll(); keepAwake(); }
  });
  window.addEventListener("pagehide", () => stopMicrophone("pagehide"));
  window.addEventListener("online", poll);
  window.addEventListener("offline", () => stopMicrophone());
  window.addEventListener("error", () => diagnostic("error"));
  window.addEventListener("unhandledrejection", () => diagnostic("error"));

  function displayParty(party) {
    $("tv").textContent = party.tv ? "TV connectée" : "TV en attente";
    $("tv").classList.toggle("online", !!party.tv);
    $("now-playing").textContent = party.player ? party.player + " · " + (party.song || "") : labels[party.phase] || "La salle est ouverte";
    const next = party.next?.[0];
    $("next-playing").textContent = next ? "Puis " + next.player + " · " + next.song : party.notice || "";
    $("preparing").hidden = party.phase !== "preparing";
    $("preparing").value = party.progress || 0;
    const index = (party.next || []).findIndex(item => item.player === state.me.name);
    $("my-queue").hidden = index < 0;
    if (index >= 0) $("my-queue").textContent = "Ton prochain morceau · " + party.next[index].song +
      " · position " + (index + 1) + " dans la file";
    $("my-turn").hidden = index !== 0;
    $("my-turn").textContent = "Ton tour approche.";
    $("players").replaceChildren(...state.participants.map(player => {
      const row = element("div", undefined, "track row between");
      row.append(element("strong", player.name), element("small", player.ready ? "Prêt" : player.count ? player.count + " chanson(s)" : player.online ? "Connecté" : "Hors ligne"));
      return row;
    }));
    $("mark").hidden = !party.recording;
    $("leave").disabled = !!state.locked;
  }
  function displaySelection() {
    const songs = state.me.songs;
    const list = $("selection");
    list.replaceChildren();
    if (!songs.length) list.append(element("p", "Ajoute un titre depuis la bibliothèque.", "muted"));
    songs.forEach((song, index) => {
      const row = trackRow(song);
      const actions = element("div", undefined, "actions");
      const move = delta => async () => {
        const ids = songs.map(item => item.id);
        [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
        await api("ordre", {songs: ids}); await poll();
      };
      const up = button("↑", move(-1)), down = button("↓", move(1));
      up.setAttribute("aria-label", "Monter " + song.title);
      down.setAttribute("aria-label", "Descendre " + song.title);
      up.disabled = state.locked || index === 0;
      down.disabled = state.locked || index === songs.length - 1;
      const remove = button("Retirer", async () => { await api("retirer-choix", {song_id: song.id}); await poll(); });
      remove.disabled = state.locked;
      actions.append(up, down, remove);
      row.append(actions);
      list.append(row);
    });
    $("ready").textContent = state.me.ready ? "Modifier ma disponibilité" : "Je suis prêt";
    $("ready").disabled = state.locked || !songs.length && !state.me.organizer;
    const mix = state.me.mix;
    $("my-mix").hidden = typeof mix?.training !== "boolean";
    const mixKey = JSON.stringify([mix, canChooseMode(state.party)]);
    if (!$("my-mix").hidden && mixKey !== myMixKey) {
      myMixKey = mixKey;
      $("my-mix-controls").replaceChildren(playerMixForm(mix, state.party,
        data => command("audio-personal", data, "reglages-joueur")));
    }
  }
  $("ready").onclick = () => run($("ready"), async () => {
    await api("pret", {ready: !state.me.ready}); await poll();
  });
  function coverImage(path) {
      const image = element("img", undefined, "cover");
      image.alt = ""; image.loading = "lazy";
      if (!coverUrls.has(path)) {
        if (coverUrls.size >= 48) {
          const oldest = coverUrls.keys().next().value;
          coverUrls.get(oldest).then(url => { if (url) URL.revokeObjectURL(url); });
          coverUrls.delete(oldest);
        }
        coverUrls.set(path, fetch(root + "/api/" + path,
          {headers: {Authorization: "Bearer " + token}}).then(response =>
          response.ok ? response.blob().then(blob => URL.createObjectURL(blob)) : null).catch(() => null));
      }
      coverUrls.get(path).then(url => { if (url) image.src = url; else image.hidden = true; });
      return image;
  }
  function trackRow(song) {
    const row = element("div", undefined, "track");
    if (song.cover) row.append(coverImage("pochette/" + song.id));
    const details = element("div", undefined, "track-info");
    const duration = Number.isFinite(song.duration) ? Math.floor(song.duration / 60) + ":" + String(Math.floor(song.duration % 60)).padStart(2, "0") : null;
    details.append(element("strong", song.title, "track-title"),
      element("small", [song.artist, song.album, duration].filter(Boolean).join(" · ")));
    if (song.lyrics !== null || song.preparation)
      details.append(element("small", [song.lyrics === true ? "Paroles minutées" : song.lyrics === false ? "Paroles à rechercher" : "", song.preparation].filter(Boolean).join(" · ")));
    row.append(details);
    return row;
  }
  async function search(reset) {
    if (!token || activeView !== "library") return;
    if (reset) { offset = 0; $("tracks").replaceChildren(); }
    const number = ++generation;
    const query = $("search").value.trim();
    try {
      const params = new URLSearchParams({q: album ? "" : query, mode: $("mode").value, offset});
      if (album) { params.set("artist", album.artist); params.set("album", album.album); }
      const result = await api("recherche", undefined, params);
      if (number !== generation || activeView !== "library") return;
      $("found").textContent = result.ready ? (album ? album.album + " · " : "") + result.total + " titre(s)" : "Chargement de la bibliothèque…";
      $("more").hidden = offset + result.results.length >= result.total;
      $("missing").hidden = !query || !!album || !result.ready || result.total !== 0;
      if (album && !offset) $("tracks").append(button("Toutes les chansons", () => { album = null; return search(true); }));
      for (const song of result.results) {
        const row = trackRow(song);
        const actions = element("div", undefined, "actions");
        if (!album && song.album) actions.append(button("Album", () => { album = song; return search(true); }));
        const pick = button("Ajouter", async () => {
          await api("choix", {song_id: song.id});
          notice("Ajouté · " + song.title); await poll();
        }, "primary");
        pick.disabled = state.locked;
        pick.dataset.pick = "1";
        actions.append(pick); row.append(actions); $("tracks").append(row);
      }
      offset += result.results.length;
    } catch (error) { if (number === generation) notice(error.message); }
  }
  $("search").oninput = () => {
    album = null;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => search(true), 220);
  };
  $("mode").onchange = () => { album = null; search(true); };
  $("more").onclick = () => run($("more"), () => search(false));
  $("missing").onclick = () => {
    const artistMode = $("mode").value === "artist";
    wantedTrack = artistMode ? "" : $("search").value.trim();
    if (artistMode) $("external-artist").value = $("search").value;
    $("external").open = true;
    show("imports");
    $("external-artist").focus();
    if (artistMode) searchExternalArtists(false);
  };
  async function loadExternal(action, data, revision, message) {
    $("external-status").textContent = message;
    try {
      const result = await api(action, data);
      if (revision !== externalRevision) return;
      $("external-status").textContent = "";
      return result;
    } catch (error) {
      if (revision === externalRevision) $("external-status").textContent = error.message;
    }
  }
  function externalRow(title, detail, label, action) {
    const row = element("div", undefined, "track");
    const info = element("div", undefined, "track-info");
    info.append(element("strong", title));
    if (detail) info.append(element("small", detail));
    row.append(info, button(label, action));
    return row;
  }
  function searchExternalArtists(submitted) {
    const query = $("external-artist").value.trim();
    const revision = ++externalRevision;
    clearTimeout(externalTimer);
    pendingArtistSearch = null;
    $("external-results").replaceChildren();
    $("external-status").textContent = "";
    if (query.length < 2) return;
    externalTimer = setTimeout(() => suggestArtists({query, revision, submitted}), submitted ? 0 : 320);
  }
  async function suggestArtists(search) {
    if (search.revision !== externalRevision) return;
    // Keep only the latest prefix waiting behind the current provider request.
    if (artistBusy) {
      pendingArtistSearch = search;
      return;
    }
    artistBusy = true;
    try {
      const artists = await loadExternal("artistes-externes", {query: search.query}, search.revision,
        "Recherche d’artistes…");
      if (!artists) return;
      if (!artists.length) {
        $("external-status").textContent = "Aucun artiste trouvé.";
        return;
      }
      const exact = artists.filter(artist => artist.name.toLowerCase() === search.query.toLowerCase());
      if (search.submitted && (exact.length === 1 || artists.length === 1)) {
        await openExternalArtist((exact.length === 1 ? exact : artists)[0]);
        return;
      }
      for (const artist of (search.submitted ? artists : artists.slice(0, 6))) {
        $("external-results").append(externalRow(artist.name, artist.followers, "Albums", () => openExternalArtist(artist)));
      }
    } finally {
      artistBusy = false;
      const next = pendingArtistSearch;
      pendingArtistSearch = null;
      if (next) suggestArtists(next);
    }
  }
  async function openExternalArtist(artist) {
    clearTimeout(externalTimer);
    const revision = ++externalRevision;
    $("external-artist").value = artist.name;
    $("external-results").replaceChildren();
    const releases = await loadExternal("albums-artiste", {artist: artist.id}, revision, "Recherche des albums…");
    if (releases) displayExternalAlbums(releases, artist);
  }
  function displayExternalAlbums(releases, artist) {
    $("external-results").replaceChildren();
    if (!releases.albums.length) {
      $("external-status").textContent = "Aucun album trouvé pour cet artiste.";
      return;
    }
    $("external-status").textContent = "";
    const artistName = releases.name || artist.name;
    const heading = element("div", undefined, "row between");
    heading.append(element("h3", artistName));
    if (releases.handle) heading.append(button("Tout télécharger", async () => {
      const count = releases.albums.length;
      const scope = count === 1 ? "l’album affiché" : "les " + count + " albums affichés";
      const action = state.me.organizer ? "Télécharger " : "Demander ";
      if (!confirm(action + scope + " de " + artistName +
          " ? Confirme que tu disposes des droits nécessaires.")) return;
      const job = await api("importer-album", {handle: releases.handle, all: true, rights: true});
      notice(job.direct ? "Lot lancé dans la file de Parole." : "Demande groupée transmise à l’organisateur.");
      await poll();
    }, "primary"));
    $("external-results").append(heading);
    for (const release of releases.albums) {
      $("external-results").append(externalRow(release.title, release.year, "Pistes",
        () => openExternalEdition(release, releases, artist)));
    }
  }
  async function openExternalEdition(release, releases, artist) {
    const revision = ++externalRevision;
    const edition = await loadExternal("edition-externe", {album: release.id}, revision, "Chargement des pistes…");
    if (!edition) return;
    const back = button("Retour aux albums", () => displayExternalAlbums(releases, artist));
    const heading = [edition.title, edition.year, edition.edition].filter(Boolean).join(" · ");
    const title = element("div", undefined, "track");
    if (edition.cover) title.append(coverImage("pochette-externe/" + edition.handle));
    title.append(element("h3", heading));
    const list = element("ol");
    const selection = element("select");
    selection.id = "external-track";
    const none = element("option", "Importer l’album seul");
    none.value = "";
    selection.append(none);
    const matches = [];
    edition.tracks.forEach((track, index) => {
      list.append(element("li", (track.disc ? "Disque " + track.disc + " · " : "") + track.title));
      const option = element("option", track.title);
      option.value = String(index);
      selection.append(option);
      if (wantedTrack && wantedTrack.localeCompare(track.title, "fr", {sensitivity: "base"}) === 0) matches.push(index);
    });
    if (matches.length === 1) selection.value = String(matches[0]);
    const label = element("label", "Piste à ajouter à ma sélection");
    label.htmlFor = selection.id;
    $("external-results").replaceChildren(back, title, element("p", edition.artists.join(", ")), list, label,
      selection, button("Demander l’import de l’album", async () => {
        if (!confirm("Confirmer l’import de l’album complet et les droits nécessaires ?")) return;
        const add = selection.value !== "";
        await api("importer-album", {handle: edition.handle, add, rights: true, ...(add ? {track: Number(selection.value)} : {})});
        notice("Demande transmise à l’organisateur.");
        await poll();
      }, "primary"));
  }
  $("external-artist").oninput = () => searchExternalArtists(false);
  $("external-form").onsubmit = event => {
    event.preventDefault();
    searchExternalArtists(true);
  };
  function displayImports(party) {
    $("external").hidden = !window.isSecureContext;
    const list = $("import-jobs");
    list.replaceChildren();
    if (!party.imports?.length) list.append(element("p", "Aucune demande d’importation.", "muted"));
    for (const job of party.imports || []) {
      const row = element("div", undefined, "track");
      const info = element("div", undefined, "track-info");
      const scope = job.tracks == null ? job.albums + (job.albums === 1 ? " album" : " albums") : job.tracks + " pistes";
      info.append(element("strong", job.title), element("small", scope + " · " + job.message));
      if (job.note) info.append(element("small", job.note));
      if (job.progress > 0 && job.progress < 1) {
        const progress = element("progress"); progress.max = 1; progress.value = job.progress; info.append(progress);
      }
      row.append(info);
      if (state.me.organizer && job.state === "approval")
        row.append(button("Autoriser", () => command("import-approve", {job: job.id})));
      if (state.me.organizer && ["approval", "queued", "running"].includes(job.state))
        row.append(button("Annuler", () => command("import-cancel", {job: job.id})));
      if (state.me.organizer && ["failed", "partial"].includes(job.state))
        row.append(button("Relancer l’échec", () => command("import-retry", {job: job.id})));
      list.append(row);
    }
  }
  function displayResults(party) {
    const scores = $("scores");
    scores.replaceChildren();
    for (const turn of party.results || []) {
      const note = turn.summary;
      const row = element("div", undefined, "track");
      const info = element("div", undefined, "track-info");
      info.append(element("strong", turn.title), element("small", note ?
        "Paroles " + note.lyrics + " % · Timing " + (note.timing ?? "—") + " % · Couverture " + note.coverage + " % · " + note.uncertain + " mot(s) incertain(s)" : turn.detail));
      row.append(info, element("span", note?.score != null ? note.score + " %" : "Sans note", "tag"));
      scores.append(row);
    }
    if (!scores.childElementCount) scores.append(element("p", "Tes résultats apparaîtront après ton tour.", "muted"));
    $("ranking").replaceChildren(...(party.ranking || []).map(row => element("p",
      row.name + " · " + (row.score != null ? row.score + " %" : "Sans note") + " · " + row.scored + "/" + row.turns + " tours notés")));
  }
  $("admin-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      await api("organisateur", {code: $("admin-code").value.trim()});
      $("admin-code").value = "";
      await poll();
    });
  };
  function canChooseMode(party) {
    return ["lobby", "ready", "finished"].includes(party.phase);
  }
  function playerMixForm(mix, party, save) {
    const form = element("form", undefined, "stack");
    const slider = (title, position, max, format, initial) => {
      const label = element("label", undefined, "mix-slider");
      const caption = element("span", undefined, "row between");
      const value = element("output", initial);
      caption.append(element("span", title), value);
      const input = element("input");
      input.type = "range"; input.min = "0"; input.max = String(max); input.step = "1";
      input.value = String(position);
      input.setAttribute("aria-label", title);
      input.setAttribute("aria-valuetext", initial);
      label.append(caption, input);
      let changed = false;
      input.oninput = () => {
        changed = true;
        value.textContent = format(Number(input.value));
        input.setAttribute("aria-valuetext", value.textContent);
      };
      return {label, position: () => Number(input.value), changed: () => changed};
    };
    const percent = value => value + " %";
    // Until a slider is moved, the server keeps the room or output default.
    const music = slider("Volume musique", mix.music ?? 100, 100, percent,
      mix.music == null ? "Volume de la salle" : percent(mix.music));
    const voice = slider("Retour du micro", mix.voice ?? 50, 100, percent,
      mix.voice == null ? "Profil de la sortie" : percent(mix.voice));
    form.append(music.label, voice.label);
    const modeLabel = element("label", undefined);
    const mode = element("input"); mode.type = "checkbox"; mode.checked = mix.training;
    mode.disabled = !canChooseMode(party);
    modeLabel.append(mode, "Entraînement sans note");
    const original = slider("Chanteur original", mix.original, 100, percent, percent(mix.original));
    const submit = element("button", "Appliquer");
    form.append(modeLabel, original.label, submit);
    form.onsubmit = event => {
      event.preventDefault();
      run(submit, async () => {
        const data = {};
        if (music.changed() && music.position() !== mix.music) data.music = music.position();
        if (voice.changed() && voice.position() !== mix.voice) data.voice = voice.position();
        if (mode.checked !== mix.training) data.training = mode.checked;
        if (original.changed() && original.position() !== mix.original) data.original = original.position();
        if (Object.keys(data).length) await save(data);
      });
    };
    return form;
  }
  function displayAdminPlayers(party) {
    const players = $("admin-players");
    const key = JSON.stringify([party.players, party.phase]);
    if (key === adminPlayersKey) return;
    adminPlayersKey = key;
    const wasOpen = players.firstElementChild?.open;
    const openDetails = Array.from(players.querySelectorAll("[data-player-mix][open]"), item => item.dataset.playerMix);
    players.replaceChildren();
    if (!party.players?.length) return;
    const details = element("details");
    details.open = !!wasOpen;
    details.append(element("summary", "Joueurs et réglages audio"));
    for (const [index, player] of party.players.entries()) {
      const row = element("div", undefined, "track");
      row.append(element("strong", player.name, "track-info"));
      if (party.phase === "lobby") {
        if (index > 0) row.append(button("Monter", () => {
          const names = party.players.map(item => item.name);
          [names[index - 1], names[index]] = [names[index], names[index - 1]];
          return command("player-order", {players: names});
        }));
        if (player.name !== state.me.name) row.append(button("Retirer", () => confirm("Retirer " + player.name + " de la partie ?") &&
          command("remove-player", {player: player.name})));
      }
      details.append(row);
      if (player.mobile && player.mix) {
        const mix = element("details");
        mix.dataset.playerMix = player.name;
        mix.open = openDetails.includes(player.name);
        mix.append(element("summary", "Réglages de " + player.name),
          playerMixForm(player.mix, party, data => command("audio-player-mix", {player: player.name, ...data})));
        details.append(mix);
      }
    }
    players.append(details);
  }
  function displayAdmin(party) {
    $("admin-form").hidden = state.me.organizer || !window.isSecureContext;
    $("admin-main").hidden = !state.me.organizer;
    if (!state.me.organizer) {
      if (!window.isSecureContext) notice("Ouvre le lien HTTPS de la salle pour administrer la partie.");
      return;
    }
    const actions = $("commands");
    actions.replaceChildren();
    const ctl = (name, action, css, confirmation) =>
      button(name, () => confirmation && !confirm(confirmation) ? undefined : command(action, confirmation ? {confirmed: true} : {}), css);
    if (["lobby", "ready"].includes(party.phase))
      actions.append(button(party.failed ? "Relancer la préparation" : "Préparer la partie",
        () => command("prepare", {retry: !!party.failed}), "primary"));
    if (party.phase === "ready") actions.append(ctl("Lancer la partie", "start", "primary"));
    if (party.can_skip) actions.append(ctl("Écarter les morceaux en échec", "skip-failed"));
    if (party.can_sing_only) actions.append(ctl("Autoriser le chant sans note", "sing-only"));
    if (party.phase === "running") {
      if (party.turn_phase === "playing") actions.append(ctl(party.paused ? "Reprendre" : "Pause", party.paused ? "resume" : "pause"));
      actions.append(ctl("Tour suivant", "next", "", "Interrompre le tour actuel ?"), ctl("Terminer", "end", "danger", "Terminer la partie ?"));
    }
    if (party.phase === "finished") actions.append(ctl("Nouvelle partie", "new", "primary"));
    displayAdminPlayers(party);
    $("admin-status").textContent = party.failed || party.notice || "";
    $("bb-status").textContent = party.blackbox?.active ? "● Collecte en cours" :
      party.blackbox?.finalizing ? "Finalisation…" : party.blackbox?.error || (party.blackbox?.latest ?
        "Dernière session · " + party.blackbox.latest.events + " événements · " + party.blackbox.latest.incidents + " incidents" +
        (party.blackbox.latest.complete ? "" : " · données partielles") : "Désactivée");
    $("bb-control").textContent = party.blackbox?.active ? "Arrêter BlackBox" : "Activer BlackBox";
    $("bb-control").disabled = !!party.blackbox?.finalizing;
    $("bb-voice-choice").hidden = !!(party.blackbox?.active || party.blackbox?.finalizing);
    $("sound-settings").hidden = !party.audio?.available;
    if (party.audio) {
      $("audio-profile").value = party.audio.profile;
      $("audio-output").value = party.audio.output;
      if (document.activeElement !== $("global-voice")) $("global-voice").value = party.audio.ceiling;
    }
  }
  $("bb-control").onclick = () => run($("bb-control"), async () => {
    if (state.party.blackbox?.active) await command("blackbox-stop");
    else {
      const voice = $("bb-voice").checked;
      const subject = voice ? "les diagnostics, les pistes vocales, le monitor de sortie et le micro témoin pendant les tours" : "les diagnostics";
      if (confirm("Confirmer l’accord des participants et des responsables des mineurs pour " + subject + " ?")) {
        await command("blackbox-start", {consent: true, voice});
        $("bb-voice").checked = false;
      }
    }
  });
  $("music-volume").onchange = () => command("music-volume", {percent: Number($("music-volume").value)}).catch(error => notice(error.message));
  $("audio-output").onchange = () => command("audio-output", {value: $("audio-output").value}).catch(error => notice(error.message));
  $("audio-profile").onchange = () => command("audio-profile", {value: $("audio-profile").value}).catch(error => notice(error.message));
  $("global-voice").onchange = () => command("audio-ceiling", {value: Number($("global-voice").value)}).catch(error => notice(error.message));
  $("micro-cut").onclick = () => run($("micro-cut"), () => command("audio-cut"));

  function render() {
    if (!state || !activeView) return;
    const party = state.party;
    const key = activeView + JSON.stringify(state);
    if (activeView !== "mic" && key === renderKey) return;
    renderKey = key;
    $("identity").textContent = state.me.name + (state.me.organizer ? " · Organisation" : "");
    $("recording").hidden = !party.recording;
    if (activeView === "party") displayParty(party);
    if (activeView === "library") for (const pick of $("tracks").querySelectorAll("[data-pick]")) pick.disabled = state.locked;
    if (activeView === "selection") displaySelection();
    if (activeView === "imports") displayImports(party);
    if (activeView === "results") displayResults(party);
    if (activeView === "admin") displayAdmin(party);
    if (activeView === "mic") displayMicrophone(party);
  }

  function displayMicrophone(party) {
    const enabled = !!party.audio?.available && window.isSecureContext && party.player === state.me.name &&
      ["awaiting-mic", "opening", "playing"].includes(party.turn_phase);
    $("micro").disabled = !enabled;
    $("micro").textContent = mic ? "Couper le micro" : "Activer le micro";
    $("micro").classList.toggle("micro-active", !!mic);
    $("micro-status").textContent = micMessage || (mic ? "Micro actif" : enabled ? "C’est ton tour. Active le micro pour chanter." :
      party.audio?.available ? "En attente de ton tour." : "Micro du téléphone indisponible.");
    $("return-volume").hidden = !enabled;
    if (!enabled) return;
    const mix = state.me.mix || {};
    const ceiling = party.audio.music_ceiling ?? 100;
    for (const [key, id, fallback] of [["music", "live-music", 100], ["original", "live-original", 0]]) {
      const input = $(id);
      if (document.activeElement !== input && !(key in liveMixPending)) input.value = String(mix[key] ?? fallback);
      const requested = Number(input.value);
      $(id + "-value").value = (key === "music" ? Math.min(requested, ceiling) : requested) + " %";
    }
    if (!mic?.ready) {
      if (document.activeElement !== $("voice") && !("voice" in liveMixPending))
        $("voice").value = String(mix.voice ?? 50);
      $("voice-applied").value = "0 % · micro coupé";
    }
    $("live-guide-note").hidden = !!mix.training || Number($("live-original").value) === 0;
  }
  let liveMixPending = {}, liveMixTimer, liveMixBusy = false;
  async function sendLiveMix() {
    if (liveMixBusy || !Object.keys(liveMixPending).length) return;
    const data = liveMixPending;
    liveMixPending = {};
    liveMixBusy = true;
    try { await command("audio-personal", data, "reglages-joueur"); }
    catch (error) { notice(error.message); }
    finally {
      liveMixBusy = false;
      if (Object.keys(liveMixPending).length) sendLiveMix();
    }
  }
  function liveMix(key, value) {
    liveMixPending[key] = value;
    if (!liveMixTimer) liveMixTimer = setTimeout(() => { liveMixTimer = null; sendLiveMix(); }, 80);
  }
  $("live-music").oninput = () => {
    const value = Number($("live-music").value);
    $("live-music-value").value = Math.min(value, state.party.audio.music_ceiling ?? 100) + " %";
    liveMix("music", value);
  };
  $("live-original").oninput = () => {
    const value = Number($("live-original").value);
    $("live-original-value").value = value + " %";
    $("live-guide-note").hidden = !!state.me.mix?.training || value === 0;
    liveMix("original", value);
  };
  function stopMicrophone(reason = "user") {
    ++micGeneration;
    if (!mic) return;
    const owner = mic;
    mic = null;
    clearInterval(owner.meter);
    if (owner.track) owner.track.getTracks().forEach(track => track.stop());
    if (owner.node) { owner.node.port.onmessage = null; owner.node.port.close(); }
    if (owner.socket) {
      if (owner.socket.readyState === WebSocket.OPEN)
        owner.socket.send(reason === "user" ? "stop" : "stop:" + reason);
      owner.socket.close();
    }
    if (owner.context) owner.context.close().catch(() => {});
    $("micro-level").value = 0;
  }
  async function startMicrophone() {
    const version = ++micGeneration;
    const owner = mic = {};
    const current = () => {
      if (version !== micGeneration || mic !== owner) throw new Error("Activation interrompue.");
    };
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio || !navigator.mediaDevices) throw new Error("Audio du navigateur indisponible.");
    try {
      const context = owner.context = new Audio({latencyHint: "interactive", sampleRate: 48000});
      const resumed = context.resume();
      const ticket = await api("micro-ticket", {});
      current();
      await resumed; current();
      const acquired = await navigator.mediaDevices.getUserMedia({audio: {
        echoCancellation: ticket.echo, noiseSuppression: ticket.noise, autoGainControl: false
      }, video: false});
      if (mic !== owner) { acquired.getTracks().forEach(track => track.stop()); current(); }
      owner.track = acquired;
      acquired.getAudioTracks()[0].onended = () => {
        if (mic === owner) { stopMicrophone("media-ended"); micMessage = "Piste micro interrompue. Réactive le micro pour reprendre."; render(); }
      };
      await context.audioWorklet.addModule(root + "/micro-worklet.js"); current();
      const url = new URL(ticket.url);
      url.searchParams.set("token", ticket.ticket); url.searchParams.set("rate", String(context.sampleRate));
      const socket = owner.socket = new WebSocket(url);
      socket.onclose = event => {
        if (mic === owner) {
          stopMicrophone();
          micMessage = event.code === 4000 ? "Micro coupé par l’organisateur ou fin du tour." :
            "Connexion micro interrompue. Réactive le micro pour reprendre.";
          render();
        }
      };
      const settings = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Le serveur audio ne répond pas.")), 5000);
        socket.onmessage = event => {
          const data = JSON.parse(event.data);
          if (data.ready) { clearTimeout(timeout); resolve(data); }
        };
        socket.onerror = () => { clearTimeout(timeout); reject(new Error("Connexion audio impossible.")); };
      });
      current();
      const processor = owner.node = new AudioWorkletNode(context, "kara-micro", {processorOptions: {monitorBlockMs: settings.block_ms}});
      processor.port.postMessage({gain: ticket.capture / 100});
      const target = context.createGain(); target.gain.value = 0;
      context.createMediaStreamSource(acquired).connect(processor);
      processor.connect(target); target.connect(context.destination);
      const limit = (Math.round(context.sampleRate * settings.block_ms / 1000) * 2 + 24) *
        settings.queue_max_ms / settings.block_ms;
      processor.port.onmessage = event => {
        if (mic !== owner || socket.readyState !== WebSocket.OPEN || socket.bufferedAmount + 24 + event.data.pcm.byteLength > limit) return;
        const frame = new Uint8Array(24 + event.data.pcm.byteLength);
        frame.set([75,84,83,50]);
        const header = new DataView(frame.buffer);
        header.setUint32(4, event.data.sequence, true);
        header.setFloat64(8, performance.now(), true);
        header.setUint32(16, event.data.endFrame % (2 ** 32), true);
        header.setUint32(20, Math.floor(event.data.endFrame / (2 ** 32)), true);
        frame.set(new Uint8Array(event.data.pcm), 24); socket.send(frame);
      };
      socket.onmessage = event => {
        const data = JSON.parse(event.data);
        if (data.levels) {
          $("micro-level").value = data.rms;
          if (owner.resetPending) {
            owner.resetPending = false;
            $("voice").value = data.requested_percent;
          }
          $("voice-applied").value = data.applied_percent + " %" +
            (data.applied_percent < data.requested_percent ? " · plafond de l’organisateur" : "");
        }
      };
      $("voice").value = ticket.volume;
      $("voice-applied").value = settings.applied_percent + " %";
      owner.meter = setInterval(() => { if (socket.readyState === WebSocket.OPEN) socket.send("levels"); }, 400);
      owner.ready = true; micMessage = "";
      const applied = acquired.getAudioTracks()[0].getSettings();
      socket.send("diagnostics:" + JSON.stringify({capture: {
        context_rate_hz: context.sampleRate, base_latency_ms: context.baseLatency * 1000,
        latency: applied.latency, sampleRate: applied.sampleRate, channelCount: applied.channelCount,
        echoCancellation: applied.echoCancellation, noiseSuppression: applied.noiseSuppression,
        autoGainControl: applied.autoGainControl
      }, client: {}}));
      context.onstatechange = () => {
        if (mic === owner && owner.ready && context.state !== "running") {
          stopMicrophone("audio-suspended");
          micMessage = "Le navigateur a suspendu l’audio. Réactive le micro pour reprendre.";
          render();
        }
      };
    } catch (error) {
      if (mic === owner) { stopMicrophone(); micMessage = error.message; }
      diagnostic(error.name === "NotAllowedError" ? "micro-permission" : "micro-interruption");
      throw error;
    } finally { render(); }
  }
  $("micro").onclick = () => run($("micro"), async () => {
    if (mic) { stopMicrophone(); micMessage = "Micro coupé."; render(); }
    else await startMicrophone();
  });
  function setVoice(value) {
    const slider = $("voice");
    slider.value = String(value);
    if (mic?.socket?.readyState === WebSocket.OPEN) mic.socket.send("volume:" + value);
    else liveMix("voice", value);
  }
  $("voice").oninput = () => setVoice(Number($("voice").value));
  $("voice-minus").onclick = () => setVoice(Math.max(0, Number($("voice").value) - 5));
  $("voice-plus").onclick = () => setVoice(Math.min(100, Number($("voice").value) + 5));
  $("voice-reset").onclick = () => {
    if (mic?.socket?.readyState !== WebSocket.OPEN) return;
    mic.resetPending = true;
    mic.socket.send("volume:reset");
  };

  if (token) poll();
  else $("connection").textContent = "Bienvenue";
})();
