// Smoke test for the camera card (ajax-video-card.js), with a small fake DOM.
//   node tests/frontend/video_card_smoke.mjs
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const CARD = path.join(here, "../../custom_components/ajax_doorbell/frontend/ajax-video-card.js");

// ------------------------------------------------------------------ fake DOM

class FakeClassList {
  constructor(el) {
    this.el = el;
  }
  get set() {
    return new Set((this.el.className || "").split(/\s+/).filter(Boolean));
  }
  contains(name) {
    return this.set.has(name);
  }
  toggle(name, force) {
    const set = this.set;
    const on = force === undefined ? !set.has(name) : Boolean(force);
    if (on) set.add(name);
    else set.delete(name);
    this.el.className = [...set].join(" ");
    return on;
  }
  add(name) {
    this.toggle(name, true);
  }
  remove(name) {
    this.toggle(name, false);
  }
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.className = "";
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this._text = "";
    this.innerHTML = "";
    this.listeners = {};
    this.classList = new FakeClassList(this);
    this.rect = { left: 0, width: 1000 };
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  appendChild(child) {
    if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((c) => c !== child);
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...items) {
    for (const item of items) this.appendChild(item);
  }
  replaceChildren(...items) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._text = "";
    for (const item of items) this.appendChild(item);
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  click(extra = {}) {
    for (const fn of this.listeners.click || []) fn({ clientX: 0, ...extra });
  }
  getBoundingClientRect() {
    return { left: this.rect.left, width: this.rect.width, top: 0, height: 36 };
  }
  attachShadow() {
    this.shadowRoot = new FakeElement("#shadow");
    return this.shadowRoot;
  }
  find(predicate) {
    for (const child of this.children) {
      if (predicate(child)) return child;
      const found = child.find(predicate);
      if (found) return found;
    }
    return null;
  }
}

globalThis.HTMLElement = class extends FakeElement {
  constructor() {
    super("ajax-video-card");
  }
  get isConnected() {
    return this._connected === true;
  }
};
globalThis.document = { createElement: (tag) => new FakeElement(tag) };
const registry = new Map();
globalThis.customElements = {
  define: (name, cls) => registry.set(name, cls),
  get: (name) => registry.get(name),
};
globalThis.window = globalThis;

const timers = [];
globalThis.setInterval = (fn, ms) => {
  timers.push({ fn, ms });
  return timers.length;
};
globalThis.clearInterval = (id) => {
  if (timers[id - 1]) timers[id - 1].cleared = true;
};

const created = [];
globalThis.loadCardHelpers = async () => ({
  createCardElement(config) {
    const el = new FakeElement("hui-picture-entity-card");
    el.config = config;
    created.push(el);
    return el;
  },
});

// -------------------------------------------------------------- fake hass

const NOW = Date.now();
const iso = (ms) => new Date(ms).toISOString();
const SEGMENTS = [
  [NOW - 3 * 3600e3, NOW - 3 * 3600e3 + 60e3],
  [NOW - 3600e3, NOW - 3600e3 + 90e3],
];
const calls = [];
const subscriptions = [];
let unsubscribed = 0;
const hass = {
  language: "fr",
  locale: { language: "fr" },
  states: {
    "camera.porte_camera_porte": { attributes: { friendly_name: "camera porte", via: "nvr" } },
    "camera.porte_ajax_doorbell": { attributes: { friendly_name: "Ajax DoorBell" } },
  },
  callWS: async (msg) => {
    calls.push(msg);
    if (msg.type === "ajax_doorbell/nvr_segments") {
      return { segments: SEGMENTS.map(([a, b]) => [iso(a), iso(b)]) };
    }
    if (msg.type === "ajax_doorbell/nvr_replay") {
      return { camera: "camera.porte_camera_porte_relecture", start: msg.start };
    }
    throw { code: "unknown", message: msg.type };
  },
  connection: {
    subscribeMessage: async (callback, msg) => {
      subscriptions.push({ callback, msg });
      return () => {
        unsubscribed += 1;
      };
    },
  },
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// ----------------------------------------------------------------- the test

await import(pathToFileURL(CARD).href);
const Card = customElements.get("ajax-video-card");
assert.ok(Card, "the element is defined");
assert.equal(window.customCards[0].type, "ajax-video-card");

// The editor offers the NVR's cameras; the stub picks one of them.
const form = Card.getConfigForm();
assert.deepEqual(form.schema[0].selector.entity.filter, { integration: "ajax_doorbell", domain: "camera" });
assert.equal(Card.getStubConfig(hass).camera, "camera.porte_camera_porte");

const card = new Card();
card.setConfig({ camera: "camera.porte_camera_porte" });
card._connected = true;
card.connectedCallback();
card.hass = hass;
await settle();
await settle();

assert.equal(subscriptions.length, 1);
assert.deepEqual(subscriptions[0].msg, { type: "ajax_doorbell/nvr_camera", entity_id: "camera.porte_camera_porte" });
assert.ok(calls.some((c) => c.type === "ajax_doorbell/nvr_segments"), "spans asked");
assert.equal(created.at(-1).config.entity, "camera.porte_camera_porte", "live view first");
assert.equal(created.at(-1).config.camera_view, "live");
assert.equal(created.at(-1).hass, hass);
const els = card._els;
assert.equal(els.badge.textContent, "Direct");
assert.ok(els.toLive.hidden, "no Live button while live");

// The integration tells: clips, recording, two detections
const detectionAt = NOW - 3600e3 + 30e3;
subscriptions[0].callback({
  name: "camera porte",
  mode: "clips",
  recording: true,
  recorded: true,
  earliest: iso(NOW - 26 * 3600e3),
  replay_camera: "camera.porte_camera_porte_relecture",
  replay_start: null,
  retention_days: 2,
  detections: [
    { id: "b", start: iso(detectionAt), end: iso(detectionAt + 8e3), kinds: ["motion", "person"], thumb: "/api/x?authSig=1" },
    { id: "a", start: iso(NOW - 3 * 3600e3 + 10e3), end: iso(NOW - 3 * 3600e3 + 12e3), kinds: ["motion"], thumb: null },
  ],
});
assert.equal(els.name.textContent, "camera porte");
assert.equal(els.mode.textContent, "Sur événement");
assert.ok(els.rec.classList.contains("on"), "recording shown");
assert.match(els.timeline.innerHTML, /class="seg"/);
assert.match(els.timeline.innerHTML, /class="mark ai"/);
assert.equal(els.list.children.length, 2);
const first = els.list.children[0];
assert.equal(first.children[0].src, "/api/x?authSig=1");
assert.equal(first.children[2].textContent, "Personne");

// A tap in a gap of the timeline plays the next clip
calls.length = 0;
const [start, end] = card._window();
const gapAt = NOW - 2 * 3600e3; // between the two clips
els.timeline.click({ clientX: ((gapAt - start) / (end - start)) * 1000 });
await settle();
await settle();
const replay = calls.find((c) => c.type === "ajax_doorbell/nvr_replay");
assert.ok(replay, "replay asked");
assert.equal(Date.parse(replay.start), SEGMENTS[1][0], "snapped to the next clip");
assert.equal(created.at(-1).config.entity, "camera.porte_camera_porte_relecture");
assert.match(els.badge.textContent, /^Relecture · /);
assert.ok(!els.toLive.hidden, "Live button while replaying");

// -10 s steps back from the moment being played
calls.length = 0;
els.back.click();
await settle();
await settle();
const back = calls.find((c) => c.type === "ajax_doorbell/nvr_replay");
assert.ok(back);
assert.ok(Date.parse(back.start) < SEGMENTS[1][0] + 2e3, "went back");

// A detection replays from a little before it
calls.length = 0;
first.click();
await settle();
await settle();
const fromDetection = calls.find((c) => c.type === "ajax_doorbell/nvr_replay");
assert.equal(Date.parse(fromDetection.start), detectionAt - 5e3);

// After the last clip there is nothing to play
calls.length = 0;
els.timeline.click({ clientX: 999 });
await settle();
assert.ok(!calls.some((c) => c.type === "ajax_doorbell/nvr_replay"), "no replay after the last clip");
assert.equal(els.note.textContent, "Pas d'enregistrement à ce moment.");
assert.ok(els.note.classList.contains("error"));

// Live again
els.toLive.click();
await settle();
await settle();
assert.equal(created.at(-1).config.entity, "camera.porte_camera_porte");
assert.equal(els.badge.textContent, "Direct");

// Continuous recording: any moment plays, never before the first recording
subscriptions[0].callback({
  name: "camera garage",
  mode: "continuous",
  recording: true,
  recorded: true,
  earliest: iso(NOW - 2 * 3600e3),
  replay_camera: "camera.porte_camera_porte_relecture",
  detections: [],
});
card._segments = [[NOW - 2 * 3600e3, NOW]];
calls.length = 0;
els.timeline.click({ clientX: 10 }); // 24 h ago: before the first recording
await settle();
await settle();
const early = calls.find((c) => c.type === "ajax_doorbell/nvr_replay");
assert.equal(Date.parse(early.start), NOW - 2 * 3600e3, "snapped to the first recording");
assert.equal(els.mode.textContent, "Continu");
assert.equal(els.list.children[0].textContent, "Aucune détection pour l'instant");

// A camera the NVR does not record: live only
subscriptions[0].callback({ name: "x", mode: "not_recorded", recording: false, recorded: false, detections: [] });
assert.ok(els.timeline.hidden && els.back.hidden, "no timeline without recordings");

// Changing the span re-reads the NVR's spans
calls.length = 0;
els.spans.children.find((b) => b.dataset.span === "1h").click();
await settle();
assert.equal(card._span, "1h");

// Leaving the page stops everything
card._connected = false;
card.disconnectedCallback();
await settle();
assert.equal(unsubscribed, 1);
assert.ok(timers.every((t) => t.cleared), "timers stopped");

console.log("OK - camera card smoke checks passed");
