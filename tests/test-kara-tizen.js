"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const directory = path.join(__dirname, "../tizen/app");
const html = fs.readFileSync(path.join(directory, "index.html"), "utf8");
const nodes = new Map();
const requests = [];
const timers = new Map();
let nextTimer = 0;

function node() {
  const attributes = new Map();
  const classes = new Set();
  return {
    textContent: "", children: [], hidden: false, style: {setProperty() {}}, classList: {add: key => classes.add(key), remove: key => classes.delete(key),
      contains: key => classes.has(key)},
    get firstChild() { return this.children[0]; },
    appendChild(child) { this.children.push(child); },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
    setAttribute(key, value) { attributes.set(key, value); },
    getAttribute(key) { return attributes.get(key); },
    removeAttribute(key) { attributes.delete(key); },
    focus() {}
  };
}
for (const match of html.matchAll(/id="([^"]+)"/g)) nodes.set(match[1], node());

class Request {
  open(method, url) { this.url = url; }
  setRequestHeader(key, value) { this[key] = value; }
  send() { requests.push(this); }
  abort() {}
  respond(status, data) {
    this.status = status;
    this.responseText = JSON.stringify(data);
    this.onloadend?.();
  }
}
const document = {
  getElementById: id => nodes.get(id),
  createElement: () => node(),
  createTextNode: text => ({textContent: text}),
  addEventListener() {}
};
vm.runInNewContext(fs.readFileSync(path.join(directory, "app.js"), "utf8"), {
  window: {KARA_DISPLAY_KEY: "fixture", addEventListener() {}}, document, XMLHttpRequest: Request, performance,
  setTimeout(callback, delay) { timers.set(++nextTimer, {callback, delay}); return nextTimer; },
  clearTimeout(id) { timers.delete(id); }
});
const step = () => {
  const [id, timer] = timers.entries().next().value;
  timers.delete(id);
  timer.callback();
  return requests.at(-1);
};
assert.match(requests[0].url, /^https:\/\/kara\.labfytools\.com:8443\/tv\/etat\?qr=0&cover=$/);
assert.equal(requests[0].Authorization, "Bearer fixture");
requests[0].respond(0);
assert.match(nodes.get("hint").textContent, /nouvelle tentative/);
assert.equal([...timers.values()][0].delay, 750);
step().respond(503);
assert.equal([...timers.values()][0].delay, 1500);
const first = { title: "Accueil", artist: "Artiste", cover_revision: "first", line: 0,
  lines: ["", "Bonjour à tous", "Suite"], highlight_chars: 7, playback_state: "Playing" };
const credits = nodes.get("attribution");
step().respond(200, {...first, cover_data: "data:image/jpeg;base64,cover", attribution: [
  {label: "Paroles d’Unison", url: "https://unison.betterlyrics.org/song/Ugr4KKK_SqY"},
  {label: "Artiste <ami>", url: "https://unison.betterlyrics.org/curator/ami"},
  {label: "Lien injecté", url: "https://lrc.red.example/s/GBUM71029604"}
]});
assert.equal(credits.hidden, false);
assert.deepEqual(credits.children.map(link => link.textContent), ["Paroles d’Unison", "Artiste <ami>"]);
assert.equal(credits.children[0].href, "https://unison.betterlyrics.org/song/Ugr4KKK_SqY");
assert.equal(nodes.get("connect").hidden, true);
assert.equal(nodes.get("title").textContent, "Accueil");
assert.equal(nodes.get("artist").textContent, "Artiste");
assert.equal(nodes.get("album-art").hidden, false);
assert.equal(nodes.get("album-art").src, "data:image/jpeg;base64,cover");
assert.equal(nodes.get("sung").textContent, "Bonjour");
assert.equal(nodes.get("pending").textContent, " à tous");
assert.match(step().url, /cover=first$/);
requests.at(-1).respond(200, {...first, playback_state: "Paused"});
assert.equal(credits.hidden, true);
assert.equal(credits.children.length, 0);
assert.equal(nodes.get("status").textContent, "En pause");
assert.equal(nodes.get("sung").textContent, "Bonjour");
step().respond(200, {...first, line: 1, lines: ["Bonjour", "Suite 😀", "Trois"], highlight_chars: 8});
assert.equal(nodes.get("sung").textContent, "Suite 😀");
assert.equal(nodes.get("next").textContent, "Trois");
assert.ok(nodes.get("timed-lyrics").classList.contains("advance"));
step().respond(200, {...first, line: 8, lines: ["Sept", "Huit", "Neuf"], highlight_chars: 0});
assert.equal(nodes.get("pending").textContent, "Huit");
assert.ok(!nodes.get("timed-lyrics").classList.contains("advance"));
step().respond(200, first);
assert.equal(nodes.get("sung").textContent, "Bonjour");
step().respond(0);
step().respond(200, {title: "Nouveau titre", cover_revision: "", lines: [],
  plain_lyrics: "Paroles simples disponibles"});
assert.equal(nodes.get("title").textContent, "Nouveau titre");
assert.equal(nodes.get("plain-lyrics").textContent, "Paroles simples disponibles");
assert.equal(nodes.get("album-art").hidden, true);
assert.ok(!nodes.has("room-qr"));

assert.match(html, /src="\$WEBAPIS\/webapis\/webapis\.js"/);
const pageEvents = {}, docEvents = {}, power = [], nativeRequests = [];
const nativeTimers = [];
const nativeDocument = {...document, hidden: false, addEventListener(event, callback) { docEvents[event] = callback; }};
class NativeRequest extends Request { send() { nativeRequests.push(this); } }
let reject = false;
let exited = false;
vm.runInNewContext(fs.readFileSync(path.join(directory, "app.js"), "utf8"), {
  window: {KARA_DISPLAY_KEY: "fixture", addEventListener(event, callback) { pageEvents[event] = callback; }},
  document: nativeDocument, XMLHttpRequest: NativeRequest, performance,
  setTimeout(callback) { nativeTimers.push(callback); return nativeTimers.length; }, clearTimeout() {},
  tizen: {application: {getCurrentApplication() { return {exit() { exited = true; }}; }}},
  webapis: {appcommon: {
    AppCommonScreenSaverState: {SCREEN_SAVER_ON: "on", SCREEN_SAVER_OFF: "off"},
    setScreenSaver(state, success, error) { power.push(state); (reject ? error : success)(); }
  }}
});
assert.deepEqual(power, ["off"]);
assert.match(nativeRequests[0].url, /event=screensaver-off$/);
pageEvents.pageshow();
assert.deepEqual(power, ["off"]);
nativeRequests.at(-1).respond(200, {...first, playback_state: "Paused", tv_close: 0});
assert.deepEqual(power, ["off"]);
nativeDocument.hidden = true;
docEvents.visibilitychange();
pageEvents.pagehide();
assert.deepEqual(power, ["off", "on"]);
nativeDocument.hidden = false;
reject = true;
docEvents.visibilitychange();
assert.match(nativeRequests.at(-1).url, /event=screensaver-error$/);
reject = false;
pageEvents.pageshow();
assert.match(nativeRequests.at(-1).url, /event=screensaver-off$/);
nativeTimers.shift()();
nativeRequests.at(-1).respond(200, {...first, tv_close: 1});
assert.equal(nodes.get("tv-closed").hidden, false);
assert.deepEqual(power, ["off", "on", "off", "off", "on"]);
docEvents.keydown({keyCode: 10009, preventDefault() {}});
assert.equal(exited, true);
assert.equal(power.at(-1), "on");
console.log("Tizen simulé : connexion, paroles, crédits et cycle de l’économiseur Samsung.");
