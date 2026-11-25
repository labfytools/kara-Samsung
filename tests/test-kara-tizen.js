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
  return {
    textContent: "", children: [], hidden: false, style: {setProperty() {}}, classList: {toggle() {}},
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
assert.match(requests[0].url, /^https:\/\/kara\.labfytools\.com:8443\/tv\/etat\?qr=1$/);
assert.equal(requests[0].Authorization, "Bearer fixture");
requests[0].respond(0);
assert.match(nodes.get("hint").textContent, /nouvelle tentative/);
assert.equal([...timers.values()][0].delay, 750);
step().respond(503);
assert.equal([...timers.values()][0].delay, 1500);
const join_url = "https://kara.labfytools.com:8443/fixture/jouer";
step().respond(200, {join_url, qr_data: "fixturePNG", organizer_pin: "0123", party_phase: "lobby"});
assert.equal(nodes.get("connect").hidden, true);
assert.equal(nodes.get("pin").textContent, "0123");
assert.equal(nodes.get("room-url").textContent, join_url);
assert.equal(nodes.get("room-qr").src, "data:image/png;base64,fixturePNG");
step().respond(200, {join_url, party_phase: "running", current_song: "Fiesta", current_player: "Alex", countdown: 2});
assert.equal(nodes.get("mobile-room").hidden, true);
assert.equal(nodes.get("current").children.at(-1).textContent, "2");
step().respond(200, {join_url, party_phase: "running", turn_phase: "playing", current_song: "Fiesta",
  lines: ["", "Paroles", "Suite"], progress: 0.5});
assert.equal(nodes.get("next").textContent, "Suite");
assert.equal(nodes.get("fill").style.width, "50%");
step().respond(0);
step().respond(200, {join_url, party_phase: "finished", ranking: [{name: "Alex", score: 91}]});
assert.match(nodes.get("status").textContent, /Alex.*91/);
step().respond(200, {join_url, organizer_pin: "0234", party_phase: "lobby"});
assert.equal(nodes.get("pin").textContent, "0234");
assert.equal(nodes.get("mobile-room").hidden, false);
console.log("Tizen simulé : démarrage, réessais, QR et PIN, compte à rebours, chant, reprise et classement.");
