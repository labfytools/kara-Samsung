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
  window: {KARA_DISPLAY_KEY: "fixture"}, document, XMLHttpRequest: Request, performance,
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
step().respond(200, {...first, cover_data: "data:image/jpeg;base64,cover"});
assert.equal(nodes.get("connect").hidden, true);
assert.equal(nodes.get("title").textContent, "Accueil");
assert.equal(nodes.get("artist").textContent, "Artiste");
assert.equal(nodes.get("album-art").hidden, false);
assert.equal(nodes.get("album-art").src, "data:image/jpeg;base64,cover");
assert.equal(nodes.get("sung").textContent, "Bonjour");
assert.equal(nodes.get("pending").textContent, " à tous");
assert.match(step().url, /cover=first$/);
requests.at(-1).respond(200, {...first, playback_state: "Paused"});
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
console.log("Tizen simulé : connexion, pochette, mots, pause, avance, déplacements et paroles simples.");
