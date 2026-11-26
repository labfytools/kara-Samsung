(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const root = location.pathname.replace(/\/jouer\/?$/, "");
  const storageKey = "kara.session." + root;
  const catalogueActions = new Set(["albums-externes"]);
  let token = localStorage.getItem(storageKey) || "";
  let state, activeView, stateTimer, searchTimer, generation = 0, offset = 0, pollRunning = false;
  let organizer = sessionStorage.getItem(storageKey + ".organizer") === "true";
  let editing = false, adding = false, picked = null, externalOpen = false, catalogueRevision;
  let importId = localStorage.getItem(storageKey + ".import"), resultCount = null, resultUntil = 0;
  let starting = false, autoMic = false, micAllowed = false, startIntent = false;
  const searchInput = $("search"), nickname = $("nickname");
  nickname.value = localStorage.getItem("kara.nickname") || "";
  searchInput.value = localStorage.getItem(storageKey + ".search") || "";
  nickname.oninput = () => localStorage.setItem("kara.nickname", nickname.value);
  let mic = null, micGeneration = 0, micMessage = "", lastInvitation;
  let externalRevision = 0;
  const coverUrls = new Map();
  let noticeTimer;
  let adminPlayersKey = "", adminKey = "", waitingKey = "", importKey = "";
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
    const id = randomId(16);
    const request = {request: id, revision: state?.party.revision, ...data};
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

  function randomId(size) {
    return Array.from(crypto.getRandomValues(new Uint8Array(size)),
      digit => digit.toString(16).padStart(2, "0")).join("");
  }
  async function enroll() {
    if (token) return;
    if (!nickname.value.trim()) { nickname.focus(); throw new Error("Indique ton prénom ou ton pseudo."); }
    const key = storageKey + ".registration";
    const registration = localStorage.getItem(key) || randomId(24);
    localStorage.setItem(key, registration);
    const member = await api("inscription", {name: nickname.value, registration});
    remember(member.token);
    localStorage.setItem("kara.nickname", member.name);
  }
  function connected(value) {
    $("connection").hidden = value;
    $("connection").textContent = value ? "" : "Reconnexion…";
  }
  function isMyTurn(party) {
    return !!state && !!party && party.player === state.me.name && party.turn_phase !== "";
  }
  let wakeLock, wakePending = false, nextWakeAttempt = 0;
  function releaseWake() {
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  }
  async function keepAwake() {
    if (!isMyTurn(state?.party) || !navigator.wakeLock || document.hidden || wakeLock ||
        wakePending || performance.now() < nextWakeAttempt) return;
    wakePending = true;
    nextWakeAttempt = performance.now() + 8000;
    try {
      const lock = await navigator.wakeLock.request("screen");
      if (!isMyTurn(state?.party) || document.hidden) { await lock.release(); return; }
      wakeLock = lock;
      lock.addEventListener("release", () => { if (wakeLock === lock) wakeLock = null; });
    } catch (_) {}
    finally { wakePending = false; }
  }
  function setImport(id) {
    importId = id;
    if (id) localStorage.setItem(storageKey + ".import", id);
    else localStorage.removeItem(storageKey + ".import");
  }
  async function poll() {
    if (!token || pollRunning) return;
    pollRunning = true;
    clearTimeout(stateTimer);
    try {
      state = await api("etat");
      connected(true);
      nickname.value = state.me.name;
      const party = state.party;
      const turn = isMyTurn(party);
      const recording = turn && ["awaiting-mic", "countdown", "opening", "playing"].includes(party.turn_phase);
      if (!recording || !party.audio?.available) stopMicrophone("room-ended");
      if (turn) {
        keepAwake();
        if (party.turn !== lastInvitation) {
          lastInvitation = party.turn;
          organizer = false;
          sessionStorage.removeItem(storageKey + ".organizer");
          autoMic = startIntent = false;
          micAllowed = true;
          micMessage = "";
          $("voice-consent").checked = false;
          nextWakeAttempt = 0;
        }
        if (party.turn_phase === "awaiting-mic" && !party.recording_voice && !autoMic && !mic) {
          autoMic = true;
          const offered = party.turn;
          navigator.permissions?.query({name: "microphone"}).then(permission => {
            if (permission.state === "granted" && lastInvitation === offered && micAllowed && !document.hidden &&
                isMyTurn(state?.party) && state.party.turn_phase === "awaiting-mic" && !mic)
              startMicrophone().catch(() => {});
          }).catch(() => {});
        }
      } else releaseWake();
      const count = party.results?.length || 0;
      if (!turn && count > resultCount && resultCount !== null) resultUntil = Date.now() + 9000;
      resultCount = count;
      if (importId && !party.imports?.some(job => job.id === importId)) {
        const finishedId = importId;
        setImport(null);
        externalOpen = false;
        editing = !!state.me.songs.length;
        api("historique-imports", undefined, "mine=1").then(history => {
          const job = history.find(item => item.id === finishedId);
          if (job) notice(job.state === "done" ? "Bibliothèque actualisée." : job.message);
        }).catch(() => {});
        search(true);
      }
      if (catalogueRevision !== state.catalogue_revision) {
        catalogueRevision = state.catalogue_revision;
        if (!externalOpen && !importId) search(true);
      }
      render();
      maybeStart();
    } catch (error) {
      connected(false);
      if (error.status === 401) {
        stopMicrophone("room-ended");
        remember("");
        state = null;
        setImport(null);
        localStorage.removeItem(storageKey + ".registration");
        organizer = false;
        sessionStorage.removeItem(storageKey + ".organizer");
        render();
        notice("Rejoins la soirée avec ton prénom.");
      }
    } finally {
      pollRunning = false;
      if (token) stateTimer = setTimeout(poll, isMyTurn(state?.party) ? 350 : 1100);
    }
  }
  $("organizer").onclick = () => {
    organizer = !organizer;
    sessionStorage.setItem(storageKey + ".organizer", String(organizer));
    render();
    if (organizer && !state?.me.organizer) {
      (token ? $("admin-code") : nickname).focus();
    } else if (!organizer) search(true);
  };
  async function mark() {
    await api("signaler", {kind: $("mark-kind").value, note: $("mark-note").value});
    $("mark-note").value = "";
    notice("Le moment a été marqué dans BlackBox.");
  }
  $("mark-live").onclick = () => run($("mark-live"), mark);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      micAllowed = startIntent = false;
      stopMicrophone("background");
      releaseWake();
      if (isMyTurn(state?.party)) micMessage = "Micro interrompu en arrière-plan. Réactive-le pour reprendre.";
    } else { nextWakeAttempt = 0; poll(); keepAwake(); }
  });
  window.addEventListener("pagehide", () => stopMicrophone("pagehide"));
  window.addEventListener("online", () => token ? poll() : search(true));
  window.addEventListener("offline", () => {
    micAllowed = startIntent = false;
    stopMicrophone("network");
    micMessage = "Le micro ne transmet plus. Réactive-le après la reconnexion.";
    connected(false); render();
  });
  window.addEventListener("error", () => diagnostic("error"));
  window.addEventListener("unhandledrejection", () => diagnostic("error"));

  function displayWaiting(party) {
    const songs = state.me.songs;
    const next = party.next || [];
    const index = next.findIndex(item => item.player === state.me.name);
    const song = (index >= 0 && songs.find(item => item.title === next[index].song)) || songs[0];
    const unscored = state.me.unscored?.includes(song?.title);
    const failed = song?.preparation === "failed" && !unscored;
    const ready = song?.preparation === "ready" || unscored;
    const running = party.phase === "running";
    const waiting = running ? "En attente de ton tour" : "En attente de l’ouverture du salon";
    $("waiting-title").textContent = failed ? "Préparation indisponible" : ready || !song ? waiting : "Chargement de ta chanson en cours…";
    if (song && !ready && !failed) {
      const spinner = element("span", undefined, "spinner");
      spinner.setAttribute("aria-hidden", "true");
      $("waiting-title").prepend(spinner, " ");
    }
    $("waiting-song").textContent = song ? [song.title, song.artist].filter(Boolean).join(" · ") : "";
    const before = index < 0 ? "" : index + (party.player ? 1 : 0) + " passage(s) avant toi";
    $("waiting-detail").textContent = [
      ready ? (unscored ? "Ta chanson est prête pour un tour sans note." : "Ta chanson est prête !") : "",
      ready || !song ? "" : waiting,
      before,
      party.player ? party.player + " chante " + party.song : "",
      party.notice
    ].filter(Boolean).join(" · ");
    $("edit-song").hidden = !song || state.locked;
    $("add-song").hidden = !song || state.locked || songs.length >= 12;
    $("retry-song").hidden = !failed;
    $("retry-song").onclick = () => run($("retry-song"), () => command("player-retry-song", {song: song.title}, "relancer-chanson"));
    const key = JSON.stringify([songs, state.locked]);
    if (waitingKey === key) return;
    waitingKey = key;
    $("selected-songs").hidden = songs.length < 2 || state.locked;
    $("selection").replaceChildren(...songs.map(item => {
      const row = trackRow(item);
      row.append(button("Retirer", async () => {
        await api("retirer-choix", {song_id: item.id});
        if (songs.length > 1) await api("pret", {ready: true});
        await poll();
      }));
      return row;
    }));
  }
  function edit(add) {
    editing = true;
    adding = add;
    picked = null;
    $("pick").disabled = true;
    render();
    search(true);
    searchInput.focus();
  }
  $("edit-song").onclick = () => edit(false);
  $("add-song").onclick = () => edit(true);
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
          {headers: token ? {Authorization: "Bearer " + token} : {}}).then(response =>
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
    row.append(details);
    return row;
  }

  async function search(reset) {
    if (externalOpen || importId || organizer || isMyTurn(state?.party)) return;
    const query = searchInput.value.trim();
    if (reset) { offset = 0; $("tracks").replaceChildren(); }
    const number = ++generation;
    $("missing").hidden = true;
    if (!query) {
      $("found").textContent = "";
      $("more").hidden = true;
      return;
    }
    try {
      const result = await api("recherche", undefined, new URLSearchParams({q: query, offset}));
      if (number !== generation || externalOpen) return;
      $("found").textContent = !result.ready ? "Chargement de la bibliothèque…" :
        result.total === 0 ? "Aucun morceau trouvé." : "";
      $("more").hidden = offset + result.results.length >= result.total;
      $("missing").hidden = !result.ready || result.total !== 0 || !!state?.locked;
      for (const song of result.results) {
        const row = trackRow(song);
        const choose = button("Choisir", () => {
          picked = song;
          $("pick").disabled = false;
          $("pick").textContent = "Valider · " + song.title;
          for (const item of $("tracks").querySelectorAll("[data-song]")) {
            const selected = item.dataset.song === song.id;
            item.classList.toggle("chosen", selected);
            item.setAttribute("aria-pressed", String(selected));
          }
        });
        choose.dataset.song = song.id;
        choose.classList.toggle("chosen", song.id === picked?.id);
        choose.setAttribute("aria-pressed", String(song.id === picked?.id));
        row.append(choose);
        $("tracks").append(row);
      }
      offset += result.results.length;
      if (!result.ready && !token) setTimeout(() => { if (number === generation) search(true); }, 1500);
    } catch (error) { if (number === generation) $("found").textContent = error.message; }
  }
  searchInput.oninput = () => {
    picked = null;
    $("pick").disabled = true;
    $("pick").textContent = "Valider ma chanson";
    localStorage.setItem(storageKey + ".search", searchInput.value);
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => search(true), 220);
  };
  $("pick").onclick = () => run($("pick"), async () => {
    if (!picked) return;
    await enroll();
    await api("choix", {song_id: picked.id, confirm: true, replace: !adding});
    picked = null;
    editing = false;
    $("pick").textContent = "Valider ma chanson";
    await poll();
  });
  $("more").onclick = () => run($("more"), () => search(false));
  $("missing").onclick = () => {
    externalOpen = true;
    $("external-title").value = searchInput.value.trim();
    render(); $("external-artist").focus();
  };
  $("external-back").onclick = () => {
    externalOpen = false; ++externalRevision; render(); search(true);
  };
  $("external-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      await enroll();
      const revision = ++externalRevision;
      $("external-status").textContent = "Recherche des albums…";
      const rows = $("external-results");
      rows.replaceChildren();
      try {
        const editions = await api("albums-externes", {title: $("external-title").value, artist: $("external-artist").value});
        if (revision !== externalRevision) return;
        $("external-status").textContent = editions.length ? "" : "Aucun album correspondant.";
        for (const edition of editions) {
          const details = element("details");
          const heading = element("summary", [edition.title, edition.artists.join(", "), edition.year, edition.edition].filter(Boolean).join(" · "));
          details.append(heading);
          if (edition.cover) details.append(coverImage("pochette-externe/" + edition.handle));
          const tracks = element("ol");
          for (const track of edition.tracks) tracks.append(element("li", track.title));
          details.append(tracks, button("Importer cet album", async () => {
            if (!confirm("Confirmer l’import de « " + edition.title + " » et l’autorisation de télécharger cet album ?")) return;
            const job = await api("importer-album", {handle: edition.handle, add: false, direct: true, rights: true});
            setImport(job.job);
            externalOpen = false;
            await poll();
          }, "primary"));
          rows.append(details);
        }
      } catch (error) { if (revision === externalRevision) $("external-status").textContent = error.message; }
      finally { poll(); }
    });
  };
  function displayImport(job) {
    const failed = ["failed", "partial"].includes(job.state);
    $("import-title").textContent = failed ? "L’album n’a pas pu être importé entièrement" : "Chargement en cours…";
    $("import-name").textContent = job.title;
    $("import-spin").hidden = failed;
    $("import-state").textContent = failed ? job.message : ({
      approval: "En attente de validation", queued: "En attente dans la file", running: "Importation de l’album",
      suspending: "Suspension en cours…", suspended: "Import suspendu · reprise en attente",
      integrating: "Actualisation de la bibliothèque", stopping: "Fin de l’import en cours"
    })[job.state] || "Préparation de l’album";
    const measured = Number.isFinite(job.progress) && job.progress > 0;
    $("import-measure").hidden = !measured;
    if (measured) $("import-measure").value = job.progress;
    $("retry-import").hidden = !failed;
    $("retry-import").onclick = () => run($("retry-import"), () => command("player-retry-import", {job: job.id}, "relancer-import"));
  }
  $("back-import").onclick = () => {
    setImport(null); externalOpen = false; editing = !!state?.me.songs.length; render(); search(true);
  };
  $("import-concurrency").onchange = event => command("import-concurrency", {batches: Number(event.target.value)}).catch(error => notice(error.message));
  let historyCursor, historyRevision, historyRequest = 0;
  async function loadHistory(reset) {
    if (!token) return;
    const request = ++historyRequest;
    const list = $("history-jobs");
    if (reset) {
      historyCursor = undefined;
      $("history-more").hidden = true;
    }
    try {
      const params = !reset && historyCursor ? new URLSearchParams({before: historyCursor}) : undefined;
      const jobs = await api("historique-imports", undefined, params);
      if (request !== historyRequest) return;
      if (reset) list.replaceChildren();
      for (const job of jobs) list.append(element("p", job.title + " · " + job.message));
      if (!jobs.length && reset) list.textContent = "Aucun import terminé.";
      if (jobs.length) historyCursor = jobs[jobs.length - 1].cursor || jobs[jobs.length - 1].id;
      $("history-more").hidden = jobs.length < 25;
    } catch (error) {
      if (request === historyRequest) list.textContent = error.message;
    }
  }
  $("history-more").onclick = () => run($("history-more"), () => loadHistory(false));
  $("import-history").ontoggle = () => { if ($("import-history").open) loadHistory(true); };
  function displayImports(party) {
    const queue = party.import_queue;
    if (queue?.batches) {
      if (queue.history_revision !== historyRevision) {
        historyRevision = queue.history_revision;
        if ($("import-history").open) loadHistory(true);
      }
      $("import-summary").textContent = [
        `${queue.active} lot(s) actif(s)`, `${queue.waiting} en attente`,
        `${queue.albums} album(s) terminé(s)`, `${queue.tracks} piste(s) traitée(s)`,
        `${queue.downloads} piste(s) en réception`,
        `${queue.download_slots}/${queue.download_ceiling} créneau(x) occupé(s)`, `${queue.processing} traitement(s)`,
        `${queue.resolving_tracks ?? 0} résolution(s) YouTube`, `${queue.integrated_per_minute ?? 0} intégrée(s)/min`,
        `${queue.errors} erreur(s)`, queue.limitation
      ].filter(Boolean).join(" · ");
      $("import-controls").hidden = !state.me.organizer;
      const select = $("import-concurrency");
      if (!select.options.length) {
        for (let number = 1; number <= queue.max_batches; number++)
          select.append(new Option(String(number), String(number)));
      }
      if (document.activeElement !== select) select.value = queue.batches;
      const pause = $("import-pause");
      const suspending = queue.pause_state === "suspending";
      pause.textContent = suspending ? "Suspension en cours…" : queue.paused ? "Reprendre la file" : "Mettre la file en pause";
      pause.disabled = suspending;
      pause.onclick = () => command(queue.paused ? "import-resume" : "import-pause");
    }
    const key = JSON.stringify([party.imports, state.me.organizer]);
    if (key === importKey) return;
    importKey = key;
    const list = $("import-jobs");
    list.replaceChildren();
    if (!party.imports?.length) list.append(element("p", "Aucune demande d’importation.", "muted"));
    for (const job of party.imports || []) {
      const row = element("div", undefined, "track");
      const info = element("div", undefined, "track-info");
      const scope = job.tracks == null ? job.albums + (job.albums === 1 ? " album" : " albums") : job.tracks + " pistes";
      info.append(element("strong", job.title), element("small", scope + " · " + job.message));
      if (job.completed || job.processed || job.errors) info.append(element("small",
        `${job.completed}/${job.albums} album(s) terminé(s) · ${job.processed} piste(s) traitée(s) · ${job.errors} erreur(s)`));
      if (job.note) info.append(element("small", job.note));
      if (job.progress > 0 && job.progress < 1) {
        const progress = element("progress"); progress.max = 1; progress.value = job.progress; info.append(progress);
      }
      if (job.remaining?.length) {
        const remaining = element("details");
        remaining.append(element("summary", "Détails du lot"));
        for (const entry of job.remaining)
          remaining.append(element("p", [entry.title, entry.message, ...entry.errors].join(" · ")));
        info.append(remaining);
      }
      row.append(info);
      if (state.me.organizer && job.state === "approval")
        row.append(button("Autoriser", () => command("import-approve", {job: job.id})));
      if (state.me.organizer && ["approval", "queued", "running", "suspended"].includes(job.state))
        row.append(button("Annuler", () => command("import-cancel", {job: job.id})));
      if (["failed", "partial"].includes(job.state))
        row.append(button("Relancer l’échec", () => state.me.organizer ? command("import-retry", {job: job.id}) :
          command("player-retry-import", {job: job.id}, "relancer-import")));
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
        note.coverage + " % des mots évalués" : turn.detail));
      row.append(info, element("span", note?.score != null ? note.score + " %" :
        note ? "Note indisponible" : "Sans note", "tag"));
      scores.append(row);
    }
    if (!scores.childElementCount) scores.append(element("p", "Tes résultats apparaîtront après ton tour.", "muted"));
    $("ranking").replaceChildren(...(party.ranking || []).map(row => element("p",
      row.name + " · " + (row.score != null ? row.score + " %" : "Sans note") + " · " + row.scored + "/" + row.turns + " tours notés")));
  }
  $("admin-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      await enroll();
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
    const imports = (party.imports || []).map(job => [job.title, job.players]);
    const key = JSON.stringify([party.players, party.phase, party.next, party.deferred,
      state.participants, party.player, party.turn_phase, imports, party.last_result?.title]);
    if (key === adminPlayersKey) return;
    adminPlayersKey = key;
    const wasOpen = players.firstElementChild?.open;
    const openDetails = Array.from(players.querySelectorAll("[data-player-mix][open]"), item => item.dataset.playerMix);
    players.replaceChildren();
    if (!party.players?.length) return;
    const details = element("details");
    details.open = wasOpen ?? true;
    details.append(element("summary", "Joueurs et réglages audio"));
    for (const [index, player] of party.players.entries()) {
      const row = element("div", undefined, "track");
      const participant = state.participants.find(item => item.name === player.name);
      const position = party.next?.findIndex(item => item.player === player.name);
      const identity = element("div", undefined, "track-info");
      identity.append(element("strong", player.name), element("small", [
        participant?.online ? "Connecté" : player.mobile ? "Hors ligne" : "Sur le bureau",
        participant?.song, participant?.preparation === "ready" ? "Prêt" : participant?.preparation === "failed" ?
          "Préparation en échec" : participant?.ready ? "Préparation en cours" : "",
        imports.find(([, names]) => names?.includes(player.name))?.[0],
        position >= 0 ? "Position " + (position + 1) : ""
      ].filter(Boolean).join(" · ")));
      row.append(identity);
      if (["lobby", "ready", "running"].includes(party.phase)) {
        if (index > 0) row.append(button("Monter", () => {
          const names = party.players.map(item => item.name);
          [names[index - 1], names[index]] = [names[index], names[index - 1]];
          return command("player-order", {players: names});
        }));
        if (player.name !== state.me.name && player.name !== party.player) row.append(button("Retirer", () => confirm("Retirer " + player.name + " de la partie ?") &&
          command("remove-player", {player: player.name})));
      }
      if (position >= 0 || player.name === party.player && ["preparing", "awaiting-mic"].includes(party.turn_phase))
        row.append(button("Reporter", () => command("defer-player", {player: player.name})));
      if (party.deferred?.includes(player.name)) row.append(button("Replacer", () => command("return-player", {player: player.name})));
      if (player.name === party.player || party.last_result?.player === player.name)
        row.append(button("Rejouer", () => confirm("Rejouer ce tour ?") && command("replay", {confirmed: true,
          player: player.name, song: player.name === party.player ? party.song : party.last_result.title})));
      details.append(row);
      if (participant?.songs?.length > 1 || participant?.preparation === "failed") {
        const songs = element("details");
        songs.append(element("summary", "Morceaux de " + player.name));
        for (const song of participant.songs) {
          const line = element("div", undefined, "track");
          const preparation = song.preparation === "ready" ? "Prêt" :
            song.preparation === "failed" ? "Préparation en échec" : "En attente de préparation";
          line.append(element("span", song.title + " · " + preparation, "track-info"));
          if (song.preparation === "failed") {
            const selection = {player: player.name, song: song.title};
            line.append(button("Relancer Turbo", () => command("retry-song", selection)),
              button("Chanter sans note", () => command("allow-unscored", selection)));
          }
          songs.append(line);
        }
        details.append(songs);
      }
      if (player.mobile && player.mix) {
        const mix = element("details");
        mix.dataset.playerMix = player.name;
        mix.open = openDetails.includes(player.name);
        mix.append(element("summary", "Réglages de " + player.name),
          playerMixForm(player.mix, party, data => command("audio-player-mix", {player: player.name, ...data})),
          button("Rétablir les valeurs par défaut", () => command("audio-reset-player", {player: player.name})));
        details.append(mix);
      }
    }
    players.append(details);
  }
  function displayAdmin(party) {
    $("admin-form").hidden = !!state?.me.organizer;
    $("admin-main").hidden = !state?.me.organizer;
    if (!state?.me.organizer) return;
    $("service-status").textContent = [
      $("connection").hidden ? "Serveur connecté" : "Serveur en reconnexion",
      party.tv ? "TV connectée" : "TV en attente", party.player && party.player + " · " + party.song
    ].filter(Boolean).join(" · ");
    const key = JSON.stringify([party.can_open, party.phase, party.turn_phase, party.paused, party.mic_receiving, party.turn]);
    const actions = $("commands");
    if (key !== adminKey) {
      adminKey = key;
      actions.replaceChildren();
      const ctl = (name, action, css, confirmation) => button(name, () =>
        confirmation && !confirm(confirmation) ? undefined : command(action, confirmation ? {confirmed: true} : {}), css);
      if (["lobby", "ready"].includes(party.phase)) {
        const open = ctl("Ouvrir le salon", "open", "primary");
        open.id = "open-room";
        open.disabled = !party.can_open;
        actions.append(open);
      }
      if (party.phase === "running") {
        if (party.turn_phase === "playing") actions.append(ctl(party.paused ? "Reprendre" : "Pause", party.paused ? "resume" : "pause"));
        if (party.turn_phase === "awaiting-mic" && party.mic_receiving)
          actions.append(button("Démarrer le tour", () => command("organizer-start", {turn: party.turn})));
        actions.append(ctl(party.turn_phase === "playing" ? "Abandonner le tour" : "Passer au suivant",
          "next", "", "Passer le tour actuel ?"),
          ctl("Terminer", "end", "danger", "Terminer la partie ?"));
      }
      if (party.phase === "finished") actions.append(ctl("Nouvelle partie", "new", "primary"));
    }
    for (const input of $("defaults-form").querySelectorAll("input")) {
      const value = party.defaults?.[input.name];
      if (document.activeElement !== input && input.dataset.dirty !== "true") input.value = value ?? "";
    }
    if (document.activeElement !== $("music-volume")) $("music-volume").value = party.audio?.music_ceiling ?? 100;
    displayAdminPlayers(party);
    $("admin-status").textContent = party.notice || (party.phase === "lobby" && !party.can_open ?
      "Le salon pourra ouvrir dès que le premier morceau sera prêt." : "");
    const disk = party.blackbox?.free_bytes;
    $("bb-disk").textContent = Number.isFinite(disk) ? (disk / 1e9).toFixed(1) + " Go disponibles" : "";
    if (party.blackbox?.unavailable?.length) $("bb-disk").textContent += " · " + party.blackbox.unavailable.join(", ");
    if (party.blackbox?.error && party.blackbox.active) $("bb-disk").textContent += " · " + party.blackbox.error;
    $("bb-preflight").textContent = Object.entries(party.blackbox?.preflight || {}).map(([name, status]) =>
      name + " · " + status).join("\n");
    $("bb-status").textContent = party.blackbox?.active ? "● Collecte en cours" :
      party.blackbox?.finalizing ? "Finalisation…" : party.blackbox?.error || (party.blackbox?.latest ?
        "Dernière session · " + party.blackbox.latest.events + " événements · " + party.blackbox.latest.incidents + " incidents" +
        (party.blackbox.latest.complete ? "" : " · données partielles") +
        (party.blackbox.verified ? " · intégrité vérifiée" : "") : "Désactivée");
    $("bb-control").textContent = party.blackbox?.active ? "Arrêter BlackBox" : "Activer BlackBox";
    $("bb-control").disabled = !!party.blackbox?.finalizing;
    $("bb-voice-choice").hidden = !!(party.blackbox?.active || party.blackbox?.finalizing);
    $("bb-mode-choice").hidden = $("bb-voice-choice").hidden || !$("bb-voice").checked;
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
      const tracks = Array.from($("bb-tracks").querySelectorAll("input:checked"), input => input.value);
      const subject = voice ? "les diagnostics et les pistes audio sélectionnées pendant les tours" : "les diagnostics";
      const pair = !voice || tracks.includes("wifi") && (tracks.includes("monitor") || tracks.includes("playback"));
      const check = pair ? "" : "\nPrévol AEC incomplet. Sélectionne le micro reçu et une référence de sortie pour les comparer.";
      if (confirm("Confirmer l’accord des participants et des responsables des mineurs pour " + subject + " ?" + check)) {
        await command("blackbox-start", {consent: true, voice, tracks, mode: voice ? $("bb-mode").value : "diagnostic_light"});
        $("bb-voice").checked = false;
        const witness = $("bb-tracks").querySelector("input[value=witness]");
        if (witness) witness.checked = false;
      }
    }
  });
  $("bb-voice").onchange = () => { $("bb-mode-choice").hidden = !$("bb-voice").checked; };
  $("bb-mode").onchange = () => {
    const mode = $("bb-mode").value;
    for (const track of $("bb-tracks").querySelectorAll("input"))
      if (track.value !== "witness") track.checked = mode !== "calibration" || ["wifi", "monitor", "playback", "return"].includes(track.value);
  };
  $("music-volume").onchange = () => command("music-volume", {percent: Number($("music-volume").value)}).catch(error => notice(error.message));
  $("audio-output").onchange = () => command("audio-output", {value: $("audio-output").value}).catch(error => notice(error.message));
  $("audio-profile").onchange = () => command("audio-profile", {value: $("audio-profile").value}).catch(error => notice(error.message));
  $("global-voice").onchange = () => command("audio-ceiling", {value: Number($("global-voice").value)}).catch(error => notice(error.message));
  $("micro-cut").onclick = () => run($("micro-cut"), () => command("audio-cut"));


  for (const [value, title] of [["wifi", "Audio reçu des téléphones"], ["whisper", "Signal envoyé au moteur de score"],
    ["turbo", "Fenêtres exactes envoyées à Turbo"], ["return", "Retour vocal numérique"], ["playback", "Mix Kara à l’entrée de la sortie"],
    ["monitor", "Monitor PipeWire"], ["witness", "Micro témoin du Vivobook"]]) {
    const label = element("label");
    const input = element("input");
    input.type = "checkbox"; input.checked = value !== "witness"; input.value = value;
    label.append(input, title); $("bb-tracks").append(label);
  }
  $("defaults-form").oninput = event => { event.target.dataset.dirty = "true"; };
  $("defaults-form").onsubmit = event => {
    event.preventDefault();
    run(event.submitter, async () => {
      const inputs = Array.from($("defaults-form").querySelectorAll("input"));
      await command("audio-defaults", Object.fromEntries(inputs.map(input => [input.name, Number(input.value)])));
      for (const input of inputs) delete input.dataset.dirty;
    });
  };

  function scoreShade(score) {
    if (!Number.isFinite(score) || score < 0 || score > 100) return "neutral";
    return score >= 90 ? "green" : score >= 70 ? "olive" : score >= 40 ? "orange" : "red";
  }
  function render() {
    const party = state?.party || {};
    const turn = isMyTurn(party);
    const job = party.imports?.find(item => item.id === importId);
    const showOrganizer = organizer && (!turn || state.me.organizer);
    const results = !turn && !editing && !job && !!state &&
      (party.phase === "finished" || Date.now() < resultUntil);
    const waiting = !turn && !results && !job && !!state &&
      !editing && (state.me.ready && state.me.songs.length || party.phase === "running" && state.locked);
    activeView = showOrganizer ? "admin" : turn ? "mic" : job ? "imports" : results ? "results" : waiting ? "party" : "library";
    $("organizer").hidden = turn && !state.me.organizer;
    $("organizer").textContent = showOrganizer ? "Retour au jeu" : "Organisateur";
    $("identity").textContent = state?.me.name || "";
    $("recording").hidden = !party.recording;
    $("nickname").hidden = $("nickname-label").hidden = !!token;
    $("registration").hidden = turn || !!job || results || waiting || showOrganizer && !!token;
    $("search-panel").hidden = externalOpen || showOrganizer;
    $("external").hidden = !externalOpen || showOrganizer;
    $("pick").disabled = !picked || !!state?.locked;
    $("loading-import").hidden = !job || turn || showOrganizer;
    $("waiting").hidden = !waiting || showOrganizer;
    $("view-mic").hidden = !turn || showOrganizer;
    $("results").hidden = !results || showOrganizer;
    $("view-admin").hidden = !showOrganizer;
    $("imports").hidden = !state || turn || !!job;
    document.body.dataset.score = scoreShade(turn && party.turn_phase === "playing" ? party.score : null);
    if (turn) displayMicrophone(party);
    if (job) displayImport(job);
    if (waiting) displayWaiting(party);
    if (results) {
      $("result-title").textContent = party.phase === "finished" ? "Résultats de la partie" : "Mon résultat";
      displayResults(party);
    }
    if (showOrganizer) displayAdmin(party);
    if (!$("imports").hidden) displayImports(party);
  }
  function displayMicrophone(party) {
    const phase = party.turn_phase;
    const enabled = !!party.audio?.available && window.isSecureContext && ["awaiting-mic", "playing"].includes(phase);
    $("micro").disabled = !enabled || starting;
    $("micro").hidden = phase === "countdown" || phase === "opening" || phase === "finishing" || phase === "playing" && !!mic;
    $("micro").textContent = phase === "playing" ? "Réactiver le micro" : starting ? "Connexion du micro…" : "Démarrer";
    $("voice-consent-label").hidden = !party.recording_voice || !enabled || !!mic;
    $("witness-consent").hidden = !party.recording_witness;
    $("turn-title").textContent = phase === "countdown" ? "Départ dans " + party.countdown :
      phase === "playing" ? "Tu chantes" : phase === "finishing" ? "Calcul du résultat…" : "À toi de chanter !";
    $("turn-song").textContent = state.me.name + " · " + (party.song || "");
    $("live-score").textContent = Number.isFinite(party.score) ? party.score + " %" : "";
    $("micro-status").textContent = micMessage || (party.mic_receiving ? "Micro connecté" :
      phase === "playing" ? "Le microphone ne transmet plus." :
      phase === "preparing" ? "Préparation du microphone…" : enabled ? "Appuie quand tu es prêt." : "");
    $("return-volume").hidden = phase === "finishing";
    $("mark-area").hidden = !party.recording;
    const mix = state.me.mix || {};
    const ceiling = party.audio?.music_ceiling ?? 100;
    for (const [key, id, fallback] of [["music", "live-music", 100], ["original", "live-original", 0]]) {
      const input = $(id);
      if (document.activeElement !== input && !(key in liveMixPending)) input.value = String(mix[key] ?? fallback);
      const requested = Number(input.value);
      $(id + "-value").value = (key === "music" ? Math.min(requested, ceiling) : requested) + " %";
    }
    if (!mic?.ready) {
      if (document.activeElement !== $("voice") && !("voice" in liveMixPending)) $("voice").value = String(mix.voice ?? 50);
      $("voice-applied").value = "0 % · micro coupé";
    }
    $("live-guide-note").hidden = !!mix.training || Number($("live-original").value) === 0;
  }
  async function maybeStart() {
    const party = state?.party;
    if (!startIntent || starting || !mic?.ready || !party?.mic_receiving || party.turn_phase !== "awaiting-mic") return;
    starting = true;
    startIntent = false;
    render();
    try { await command("player-start", {turn: party.turn}, "demarrer"); }
    catch (error) { micMessage = error.message; }
    finally { starting = false; render(); }
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
    $("live-music-value").value = Math.min(value, state.party.audio?.music_ceiling ?? 100) + " %";
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
    try {
      if (!Audio || !navigator.mediaDevices) throw new Error("Audio du navigateur indisponible.");
      const context = owner.context = new Audio({latencyHint: "interactive", sampleRate: 48000});
      const resumed = context.resume();
      const ticket = await api("micro-ticket", {consent: $("voice-consent").checked});
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
      const counters = {sent: 0, dropped: 0, buffered_bytes_max: 0};
      processor.port.onmessage = event => {
        if (mic !== owner) return;
        const queued = socket.bufferedAmount + 24 + event.data.pcm.byteLength;
        counters.buffered_bytes_max = Math.max(counters.buffered_bytes_max, queued);
        if (socket.readyState !== WebSocket.OPEN || queued > limit) {
          counters.dropped++;
          return;
        }
        const frame = new Uint8Array(24 + event.data.pcm.byteLength);
        frame.set([75,84,83,50]);
        const header = new DataView(frame.buffer);
        header.setUint32(4, event.data.sequence, true);
        header.setFloat64(8, performance.now(), true);
        header.setUint32(16, event.data.endFrame % (2 ** 32), true);
        header.setUint32(20, Math.floor(event.data.endFrame / (2 ** 32)), true);
        frame.set(new Uint8Array(event.data.pcm), 24); socket.send(frame);
        counters.sent++;
      };
      socket.onmessage = event => {
        const received = performance.now();
        const data = JSON.parse(event.data);
        if (Number.isSafeInteger(data.clock_probe) && socket.readyState === WebSocket.OPEN) {
          const begin = performance.now();
          const frame = Math.round(context.currentTime * context.sampleRate);
          const end = performance.now();
          socket.send("clock:" + JSON.stringify({id: data.clock_probe, received_ms: received,
            context_begin_ms: begin, context_end_ms: end, context_frame: frame, sent_ms: performance.now()}));
        }
        if (data.levels) {
          $("micro-level").value = data.rms;
          if (owner.resetPending) {
            owner.resetPending = false;
            $("voice").value = data.requested_percent;
          }
          $("voice-applied").value = data.applied_percent + " %" + (data.return_muted ? " · retour coupé" :
            data.return_started === false ? " · en attente du départ" :
              data.applied_percent < data.requested_percent ? " · plafond de l’organisateur" : "");
        }
      };
      $("voice").value = ticket.volume;
      $("voice-applied").value = settings.applied_percent + " %";
      owner.ready = true; micMessage = "";
      const agent = navigator.userAgent || "";
      const platform = /Android/.test(agent) ? "android" : /iPhone|iPad|iPod/.test(agent) ? "ios" : "other";
      const browser = /Firefox|FxiOS/.test(agent) ? "firefox" : /Chrome|Chromium|CriOS/.test(agent) ?
        "chromium" : /Safari/.test(agent) ? "safari" : "other";
      function sendDiagnostics() {
        const applied = acquired.getAudioTracks()[0].getSettings();
        socket.send("diagnostics:" + JSON.stringify({capture: {
          client_revision: 3, platform, browser, requested_echo: ticket.echo, requested_noise: ticket.noise,
          requested_auto_gain: false, context_rate_hz: context.sampleRate, base_latency_ms: context.baseLatency * 1000,
          latency: applied.latency, sampleRate: applied.sampleRate, channelCount: applied.channelCount,
          echoCancellation: applied.echoCancellation, noiseSuppression: applied.noiseSuppression,
          autoGainControl: applied.autoGainControl
        }, client: {...counters, buffered_bytes: socket.bufferedAmount}}));
      }
      let nextDiagnostics = performance.now() + 4000;
      owner.meter = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        socket.send("levels");
        if (state?.party.recording && performance.now() >= nextDiagnostics) {
          sendDiagnostics();
          nextDiagnostics = performance.now() + 4000;
        }
      }, 400);
      sendDiagnostics();
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
    micAllowed = true;
    micMessage = "";
    startIntent = state?.party.turn_phase === "awaiting-mic";
    if (!mic) await startMicrophone();
    await poll();
    maybeStart();
  });
  $("voice").oninput = () => {
    const value = Number($("voice").value);
    if (mic?.socket?.readyState === WebSocket.OPEN) mic.socket.send("volume:" + value);
    else liveMix("voice", value);
  };
  $("return-cut").onclick = () => run($("return-cut"), () =>
    command("audio-return-cut", {}, "couper-retour"));
  render();
  if (token) poll();
  else search(true);
})();
