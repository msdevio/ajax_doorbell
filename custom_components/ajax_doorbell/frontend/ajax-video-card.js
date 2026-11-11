/*
 * Ajax DoorBell & Cameras: card for the cameras an Ajax NVR records.
 *
 * Live video and replays play in Home Assistant's own picture card, which
 * picks WebRTC or HLS for the browser and handles H.264 as well as H.265.
 * This card adds what the NVR knows: a timeline of what it recorded (one bar
 * when it records continuously, clips when it records on events), the
 * detections with their picture, and replay controls. Nothing is stored in
 * Home Assistant except the times of the detections.
 */

const VIDEO_CARD_VERSION = "0.5.0";
const DOMAIN = "ajax_doorbell";
const SPANS = { "1h": 3600e3, "6h": 6 * 3600e3, "24h": 24 * 3600e3 };
const TICKS = { "1h": 10 * 60e3, "6h": 3600e3, "24h": 3 * 3600e3 };
const SEGMENTS_EVERY = 60e3;
const BEFORE = 5e3; // a detection is replayed from a little before
const STEP = 10e3; // the -10 s / +10 s buttons
const LATEST = 3e3; // a replay never asks for the last seconds: the NVR is still writing them
const SHOWN = 30; // detections listed with their picture

const STRINGS = {
  en: {
    live: "Live",
    replay: "Replay",
    back: "−10 s",
    forward: "+10 s",
    to_live: "Live",
    continuous: "Continuous",
    clips: "On events",
    unknown: "Measuring…",
    not_recorded: "Not recorded",
    recording: "Recording",
    detections: "Detections",
    none: "No detection yet",
    motion: "Motion",
    person: "Person",
    vehicle: "Vehicle",
    pet: "Animal",
    no_camera: "Choose a camera of the Ajax NVR in the card's settings.",
    not_nvr: "This camera is not one of an Ajax NVR's cameras.",
    nothing: "No recording at that moment.",
    no_recording: "The NVR does not record this camera: live view only.",
    failed: "Replay failed: {message}",
    hint: "Tap the timeline to replay a moment.",
  },
  fr: {
    live: "Direct",
    replay: "Relecture",
    back: "−10 s",
    forward: "+10 s",
    to_live: "Direct",
    continuous: "Continu",
    clips: "Sur événement",
    unknown: "Mesure en cours…",
    not_recorded: "Non enregistrée",
    recording: "Enregistre",
    detections: "Détections",
    none: "Aucune détection pour l'instant",
    motion: "Mouvement",
    person: "Personne",
    vehicle: "Véhicule",
    pet: "Animal",
    no_camera: "Choisis une caméra du NVR Ajax dans les réglages de la carte.",
    not_nvr: "Cette caméra n'est pas une caméra du NVR Ajax.",
    nothing: "Pas d'enregistrement à ce moment.",
    no_recording: "Le NVR n'enregistre pas cette caméra : direct seulement.",
    failed: "Relecture impossible : {message}",
    hint: "Touche la chronologie pour revoir un moment.",
  },
};

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head { display: flex; align-items: center; gap: 8px; padding: 12px 16px 8px; flex-wrap: wrap; }
  .name { font-size: 1.1em; font-weight: 500; color: var(--primary-text-color); margin-right: auto; }
  .chip { font-size: 0.8em; padding: 2px 8px; border-radius: 10px;
    background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .rec { color: var(--error-color, #db4437); font-weight: 500; display: none; }
  .rec.on { display: inline; }
  .video { position: relative; background: #000; min-height: 120px; }
  .video > * { display: block; }
  .badge { position: absolute; top: 8px; left: 8px; z-index: 2; font-size: 0.8em; color: #fff;
    background: rgba(0, 0, 0, 0.55); padding: 2px 8px; border-radius: 10px; pointer-events: none; }
  .badge.replay { background: var(--primary-color); }
  .bar { display: flex; align-items: center; gap: 8px; padding: 8px 16px 0; }
  .bar .label { margin-right: auto; color: var(--secondary-text-color); font-size: 0.9em; }
  button { font: inherit; border: none; border-radius: 16px; padding: 6px 12px; cursor: pointer;
    background: var(--secondary-background-color); color: var(--primary-text-color); }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  button[hidden] { display: none; }
  .spans { display: flex; gap: 4px; }
  .spans button { padding: 2px 10px; font-size: 0.85em; }
  .spans button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .timeline { position: relative; margin: 8px 16px 0; height: 36px; cursor: pointer; touch-action: none; }
  .timeline svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .timeline .track { fill: var(--divider-color, rgba(127, 127, 127, 0.3)); }
  .timeline .seg { fill: var(--primary-color); opacity: 0.55; }
  .timeline .mark { fill: var(--secondary-text-color); }
  .timeline .mark.ai { fill: var(--accent-color, #ff9800); }
  .timeline .cursor { fill: var(--error-color, #db4437); }
  .ticks { position: relative; height: 16px; margin: 2px 16px 0; font-size: 0.75em;
    color: var(--secondary-text-color); }
  .ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
  .note { padding: 6px 16px 0; font-size: 0.85em; color: var(--secondary-text-color); min-height: 1.2em; }
  .note.error { color: var(--error-color, #db4437); }
  .list-title { padding: 10px 16px 4px; font-weight: 500; color: var(--primary-text-color); }
  .list { display: flex; gap: 8px; overflow-x: auto; padding: 0 16px 16px; }
  .item { flex: 0 0 auto; width: 120px; padding: 0; background: none; border-radius: 8px; text-align: left; }
  .item .pic { width: 120px; height: 68px; border-radius: 8px; background: var(--secondary-background-color);
    object-fit: cover; display: block; }
  .item .when { display: block; font-size: 0.8em; color: var(--primary-text-color); padding-top: 2px; }
  .item .what { display: block; font-size: 0.75em; color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.85em; padding: 0 16px 16px; }
  .message { padding: 16px; color: var(--secondary-text-color); }
`;

class AjaxVideoCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._info = null; // what the integration says about the camera
    this._segments = []; // [[start ms, end ms]] the NVR holds video for
    this._span = "24h";
    this._mode = "live"; // or "replay"
    this._replayAt = 0; // the moment the replay started from (ms)
    this._replayClock = 0; // when it started (ms, local clock)
    this._video = null; // Home Assistant's picture card playing the stream
    this._videoEntity = null;
    this._unsub = null;
    this._timers = [];
    this._started = false;
    this._note = "";
    this._noteError = false;
    this._failed = "";
    this._els = null;
  }

  static getConfigForm() {
    return {
      schema: [
        {
          name: "camera",
          required: true,
          selector: { entity: { filter: { integration: DOMAIN, domain: "camera" } } },
        },
        { name: "title", selector: { text: {} } },
        {
          name: "span",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "1h", label: "1 h" },
                { value: "6h", label: "6 h" },
                { value: "24h", label: "24 h" },
              ],
            },
          },
        },
      ],
      computeLabel: (schema) =>
        ({ camera: "Camera / Caméra", title: "Title / Titre", span: "Timeline / Chronologie" })[
          schema.name
        ],
    };
  }

  static getStubConfig(hass) {
    const camera = Object.keys(hass.states).find(
      (id) => id.startsWith("camera.") && hass.states[id].attributes.via === "nvr"
    );
    return { camera: camera || "", span: "24h" };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    const camera = this._config && this._config.camera;
    this._config = { span: "24h", ...config };
    this._span = SPANS[this._config.span] ? this._config.span : "24h";
    if (this._started && camera !== this._config.camera) {
      this._stop();
      this._start();
    } else if (this._els) {
      this._renderAll();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (this._video) this._video.hass = hass;
    if (!this._started && this.isConnected && this._config) this._start();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this._started && this._hass && this._config) this._start();
  }

  disconnectedCallback() {
    this._stop();
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  // ------------------------------------------------------------ lifecycle

  _t(key, values) {
    const lang = ((this._hass && (this._hass.locale?.language || this._hass.language)) || "en")
      .slice(0, 2)
      .toLowerCase();
    let text = (STRINGS[lang] || STRINGS.en)[key] || STRINGS.en[key] || key;
    for (const [name, value] of Object.entries(values || {})) text = text.replace(`{${name}}`, value);
    return text;
  }

  _start() {
    this._started = true;
    this._build();
    if (!this._config.camera) {
      this._message(this._t("no_camera"));
      return;
    }
    this._mode = "live";
    this._showVideo(this._config.camera);
    this._subscribe();
    this._loadSegments();
    this._timers.push(setInterval(() => this._tick(), 1000));
    this._timers.push(setInterval(() => this._loadSegments(), SEGMENTS_EVERY));
  }

  _stop() {
    this._started = false;
    for (const timer of this._timers) clearInterval(timer);
    this._timers = [];
    const unsub = this._unsub;
    this._unsub = null;
    if (unsub) Promise.resolve(unsub).then((fn) => typeof fn === "function" && fn()).catch(() => {});
    this._video = null;
    this._videoEntity = null;
    if (this._els) this._els.video.replaceChildren();
  }

  async _subscribe() {
    const camera = this._config.camera;
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (info) => {
          if (this._config && this._config.camera === camera) {
            this._info = info;
            this._renderAll();
          }
        },
        { type: `${DOMAIN}/nvr_camera`, entity_id: camera }
      );
    } catch (err) {
      this._message(this._t("not_nvr"));
    }
  }

  async _loadSegments() {
    if (!this._hass || !this._config || !this._config.camera) return;
    if (this._info && !this._info.recorded) return;
    const end = Date.now();
    const start = end - SPANS[this._span];
    try {
      const result = await this._hass.callWS({
        type: `${DOMAIN}/nvr_segments`,
        entity_id: this._config.camera,
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
      });
      this._segments = (result.segments || []).map(([a, b]) => [Date.parse(a), Date.parse(b)]);
    } catch (err) {
      this._segments = [];
    }
    this._renderTimeline();
  }

  // ------------------------------------------------------------- playback

  async _showVideo(entity) {
    this._videoEntity = entity;
    let card = null;
    try {
      const helpers = window.loadCardHelpers ? await window.loadCardHelpers() : null;
      if (helpers && this._videoEntity === entity) {
        card = helpers.createCardElement({
          type: "picture-entity",
          entity,
          camera_view: "live",
          show_name: false,
          show_state: false,
          tap_action: { action: "none" },
          hold_action: { action: "none" },
          double_tap_action: { action: "none" },
        });
      }
    } catch (err) {
      card = null;
    }
    if (this._videoEntity !== entity || !this._els) return; // another one was asked meanwhile
    if (!card) {
      card = document.createElement("img");
      card.style.width = "100%";
      const state = this._hass && this._hass.states[entity];
      if (state && state.attributes.entity_picture) card.src = state.attributes.entity_picture;
    } else {
      card.hass = this._hass;
    }
    this._video = card;
    this._els.video.replaceChildren(this._els.badge, card);
    this._renderPlayback();
  }

  _position() {
    return this._replayAt + (Date.now() - this._replayClock);
  }

  // Move a moment to where the NVR holds video: inside a span, else the next one.
  _snap(moment) {
    const latest = Date.now() - LATEST;
    let at = Math.min(moment, latest);
    const info = this._info || {};
    const earliest = info.earliest ? Date.parse(info.earliest) : null;
    if (earliest && at < earliest) at = earliest;
    const spans = this._segments;
    if (!spans.length) return at;
    if (spans.some(([a, b]) => at >= a && at < b)) return at;
    const next = spans.find(([a]) => a > at);
    if (next) return Math.min(next[0], latest);
    if (info.mode === "continuous") return at; // the newest video may not be listed yet
    return null;
  }

  async _replay(moment) {
    const at = this._snap(moment);
    if (at === null) {
      this._say(this._t("nothing"), true);
      return;
    }
    try {
      const result = await this._hass.callWS({
        type: `${DOMAIN}/nvr_replay`,
        entity_id: this._config.camera,
        start: new Date(at).toISOString(),
      });
      this._mode = "replay";
      this._replayAt = Date.parse(result.start);
      this._replayClock = Date.now();
      this._say("");
      await this._showVideo(result.camera);
    } catch (err) {
      this._say(this._t("failed", { message: (err && (err.message || err.code)) || err }), true);
    }
    this._renderPlayback();
    this._renderTimeline();
  }

  _live() {
    this._mode = "live";
    this._say("");
    this._showVideo(this._config.camera);
    this._renderPlayback();
    this._renderTimeline();
  }

  _step(delta) {
    const from = this._mode === "replay" ? this._position() : Date.now() - LATEST;
    this._replay(from + delta);
  }

  _tick() {
    if (this._mode === "replay") {
      this._renderPlayback();
      this._renderTimeline();
    }
  }

  _say(text, error = false) {
    this._note = text;
    this._noteError = error;
    if (this._els) {
      this._els.note.textContent = text || (this._info && this._info.recorded ? this._t("hint") : "");
      this._els.note.classList.toggle("error", Boolean(text) && error);
    }
  }

  // ------------------------------------------------------------- rendering

  _build() {
    if (this._els) return;
    const root = this.shadowRoot;
    const make = (tag, className, parent, text) => {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text !== undefined) el.textContent = text;
      if (parent) parent.appendChild(el);
      return el;
    };
    root.replaceChildren();
    make("style", "", root, STYLE);
    const card = make("ha-card", "", root);
    const head = make("div", "head", card);
    const els = {
      name: make("span", "name", head),
      rec: make("span", "rec", head),
      mode: make("span", "chip mode", head),
    };
    els.video = make("div", "video", card);
    els.badge = make("span", "badge", els.video);
    const bar = make("div", "bar", card);
    els.label = make("span", "label", bar);
    els.back = make("button", "back", bar);
    els.forward = make("button", "forward", bar);
    els.toLive = make("button", "primary to-live", bar);
    els.spansBar = make("div", "bar spans-bar", card);
    make("span", "label", els.spansBar);
    els.spans = make("span", "spans", els.spansBar);
    els.timeline = make("div", "timeline", card);
    els.ticks = make("div", "ticks", card);
    els.note = make("div", "note", card);
    els.listTitle = make("div", "list-title", card);
    els.list = make("div", "list", card);
    els.message = make("div", "message", card);
    els.message.hidden = true;
    this._els = els;
    els.back.addEventListener("click", () => this._step(-STEP));
    els.forward.addEventListener("click", () => this._step(STEP));
    els.toLive.addEventListener("click", () => this._live());
    els.timeline.addEventListener("click", (ev) => this._timelineClick(ev));
    for (const span of Object.keys(SPANS)) {
      const button = make("button", "", els.spans, span.replace("h", " h"));
      button.dataset.span = span;
      button.addEventListener("click", () => {
        this._span = span;
        this._renderAll();
        this._loadSegments();
      });
    }
    this._renderAll();
  }

  _message(text) {
    if (!this._els) return;
    this._els.message.hidden = !text;
    this._els.message.textContent = text || "";
  }

  _renderAll() {
    if (!this._els) return;
    const info = this._info || {};
    const state = this._hass && this._config && this._hass.states[this._config.camera];
    this._els.name.textContent =
      (this._config && this._config.title) ||
      info.name ||
      (state && state.attributes.friendly_name) ||
      "";
    this._els.rec.textContent = `● ${this._t("recording")}`;
    this._els.rec.classList.toggle("on", info.recording === true);
    this._els.mode.textContent = info.mode ? this._t(info.mode) : "";
    this._els.mode.hidden = !info.mode;
    for (const button of this._els.spans.children) {
      button.classList.toggle("on", button.dataset.span === this._span);
    }
    const recorded = info.recorded !== false;
    for (const el of [this._els.timeline, this._els.ticks, this._els.spansBar]) {
      el.hidden = !recorded;
    }
    this._els.back.hidden = !recorded;
    this._els.forward.hidden = !recorded;
    if (!recorded) this._say(this._t("no_recording"));
    else this._say(this._note, this._noteError);
    this._renderPlayback();
    this._renderTimeline();
    this._renderList();
  }

  _clock(ms) {
    return new Date(ms).toLocaleTimeString(this._hass?.locale?.language || undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  _renderPlayback() {
    if (!this._els) return;
    const replay = this._mode === "replay";
    this._els.badge.textContent = replay
      ? `${this._t("replay")} · ${this._clock(this._position())}`
      : this._t("live");
    this._els.badge.classList.toggle("replay", replay);
    this._els.label.textContent = replay ? this._clock(this._position()) : this._t("live");
    this._els.toLive.textContent = this._t("to_live");
    this._els.toLive.hidden = !replay;
    this._els.back.textContent = this._t("back");
    this._els.forward.textContent = this._t("forward");
    this._els.forward.disabled = !replay;
  }

  _window() {
    const end = Date.now();
    return [end - SPANS[this._span], end];
  }

  _renderTimeline() {
    if (!this._els) return;
    const [start, end] = this._window();
    const width = 1000;
    const x = (ms) => ((ms - start) / (end - start)) * width;
    const parts = [`<rect class="track" x="0" y="10" width="${width}" height="16" rx="3"/>`];
    for (const [a, b] of this._segments) {
      if (b < start || a > end) continue;
      const x1 = Math.max(0, x(a));
      const x2 = Math.min(width, x(b));
      parts.push(
        `<rect class="seg" x="${x1.toFixed(1)}" y="10" width="${Math.max(1.5, x2 - x1).toFixed(1)}" height="16"/>`
      );
    }
    for (const detection of (this._info && this._info.detections) || []) {
      const at = Date.parse(detection.start);
      if (at < start || at > end) continue;
      const ai = (detection.kinds || []).some((kind) => kind !== "motion");
      parts.push(
        `<rect class="mark${ai ? " ai" : ""}" x="${(x(at) - 1).toFixed(1)}" y="${ai ? 2 : 6}" width="2" height="${ai ? 32 : 24}"/>`
      );
    }
    if (this._mode === "replay") {
      parts.push(`<rect class="cursor" x="${(x(this._position()) - 2).toFixed(1)}" y="0" width="4" height="36"/>`);
    }
    this._els.timeline.innerHTML = `<svg viewBox="0 0 ${width} 36" preserveAspectRatio="none">${parts.join("")}</svg>`;
    const step = TICKS[this._span];
    const ticks = [];
    for (let at = Math.ceil(start / step) * step; at <= end; at += step) {
      const left = ((at - start) / (end - start)) * 100;
      if (left < 4 || left > 96) continue;
      const label = new Date(at).toLocaleTimeString(this._hass?.locale?.language || undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
      ticks.push(`<span style="left:${left.toFixed(2)}%">${label}</span>`);
    }
    this._els.ticks.innerHTML = ticks.join("");
  }

  _timelineClick(ev) {
    const rect = this._els.timeline.getBoundingClientRect();
    if (!rect.width) return;
    const [start, end] = this._window();
    const ratio = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
    this._replay(start + ratio * (end - start));
  }

  _kindLabel(kinds) {
    const main = ["person", "vehicle", "pet"].find((kind) => kinds.includes(kind)) || "motion";
    return this._t(main);
  }

  _renderList() {
    if (!this._els) return;
    const info = this._info || {};
    const list = this._els.list;
    this._els.listTitle.textContent = this._t("detections");
    const detections = (info.detections || []).slice(0, SHOWN);
    const key = detections.map((d) => `${d.id}:${d.end}:${(d.kinds || []).join(",")}`).join("|");
    if (key === this._listKey) return; // nothing new: keep the pictures loaded
    this._listKey = key;
    list.replaceChildren();
    if (!detections.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = this._t("none");
      list.appendChild(empty);
      return;
    }
    for (const detection of detections) {
      const button = document.createElement("button");
      button.className = "item";
      const picture = document.createElement(detection.thumb ? "img" : "div");
      picture.className = "pic";
      if (detection.thumb) {
        picture.loading = "lazy";
        picture.alt = "";
        picture.src = detection.thumb;
      }
      const when = document.createElement("span");
      when.className = "when";
      when.textContent = new Date(detection.start).toLocaleTimeString(
        this._hass?.locale?.language || undefined,
        { hour: "2-digit", minute: "2-digit" }
      );
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = this._kindLabel(detection.kinds || []);
      button.append(picture, when, what);
      button.addEventListener("click", () => this._replay(Date.parse(detection.start) - BEFORE));
      list.appendChild(button);
    }
  }
}

if (!customElements.get("ajax-video-card")) {
  customElements.define("ajax-video-card", AjaxVideoCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "ajax-video-card")) {
  window.customCards.push({
    type: "ajax-video-card",
    name: "Ajax Camera",
    description: "Live view, replays and timeline of a camera recorded by the Ajax NVR.",
    preview: false,
    documentationURL: "https://github.com/msdevio/ajax_doorbell",
  });
}
console.info(`%c AJAX-VIDEO-CARD %c ${VIDEO_CARD_VERSION} `, "color:#fff;background:#0a7", "");
