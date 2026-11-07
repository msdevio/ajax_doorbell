/*
 * Ajax DoorBell Card
 *
 * A Lovelace intercom card for the Ajax DoorBell (custom integration
 * `ajax_doorbell`): live video, listen, talk, incoming-call screen,
 * answer / hang up. One file, no build step, no dependency.
 *
 * The stream comes from Home Assistant's built-in go2rtc (RTSP from the
 * doorbell) through HA's native camera WebRTC websocket API. Video is H.264
 * copied as is, the doorbell's audio arrives as Opus, and the browser
 * microphone goes back to the doorbell over the ONVIF backchannel.
 *
 * Config:
 *   type: custom:ajax-doorbell-card
 *   camera: camera.ajax_doorbell               # required
 *   call_entity: sensor.ajax_doorbell_call     # optional, found on the camera's device
 *   name: Entrée                               # optional, default: the camera's name
 *   answered_by: Tablette de l'entrée          # optional, default: "<user> (<device>)"
 *   two_way_audio: true                        # optional, force talk-back on (true) or off (false)
 *   push_to_talk: false                        # hold a button to talk instead of a mic toggle
 *   preview_on_ring: true                      # start the video muted as soon as it rings
 *   ring_sound: false                          # two-tone ringtone generated with Web Audio
 *   fullscreen_on_ring: false                  # take the whole screen while it rings
 *   show_stats: false                          # RTT, jitter, loss, resolution, codecs
 *   auto_retry: true                           # one automatic retry when the connection fails
 *   aspect_ratio: "16 / 9"                     # optional, default: from the video
 *
 * Answer from a notification: open the dashboard with ?ajax_answer=1 (or
 * ?ajax_answer=camera.xxx when there are several doorbells), e.g. companion
 * app notification data `clickAction` (Android) / `url` (iOS):
 *   /lovelace/entree?ajax_answer=1
 *
 * The microphone needs a secure context (HTTPS or http://localhost). Without
 * it the card still shows the video and plays the sound.
 */

const CARD_VERSION = "0.1.0";
const DOMAIN = "ajax_doorbell";

const KEEPALIVE_MS = 60000; // re-answer during our call: the integration ends unattended calls
const POSTER_REFRESH_MS = 10000;
const CONNECT_TIMEOUT_MS = 20000;
const DISCONNECTED_GRACE_MS = 6000;
const DETACH_GRACE_MS = 1500; // Lovelace re-parents cards (masonry reflow): keep the call alive
const RETRY_DELAY_MS = 1200;
const STABLE_MS = 30000; // a connection up this long earns a new automatic retry
const STATS_MS = 2000;
const FLASH_MS = 3500;
const RING_REPEAT_MS = 3000;
const FS_FALLBACK_MS = 1000;
const TALK_READY_MS = 8000;

const MIC_CONSTRAINTS = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  video: false,
};

/* Source of the talk-back AudioWorklet (G.722 / G.711 encoders, bit-exact with FFmpeg). */
const TALK_WORKLET = `
function createG722Encoder() {
  const QMF = [3, -11, 12, 32, -210, 951, 3876, -805, 362, -156, 53, -11];
  const Q6 = [0, 35, 72, 110, 150, 190, 233, 276, 323, 370, 422, 473, 530, 587, 650, 714, 786, 858,
    940, 1023, 1121, 1219, 1339, 1458, 1612, 1765, 1980, 2195, 2557, 2919, 0, 0];
  const ILN = [0, 63, 62, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14,
    13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 0];
  const ILP = [0, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42,
    41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 0];
  const WL = [-60, -30, 58, 172, 334, 538, 1198, 3042];
  const RL42 = [0, 7, 6, 5, 4, 3, 2, 1, 7, 6, 5, 4, 3, 2, 1, 0];
  const ILB = [2048, 2093, 2139, 2186, 2233, 2282, 2332, 2383, 2435, 2489, 2543, 2599, 2656, 2714,
    2774, 2834, 2896, 2960, 3025, 3091, 3158, 3228, 3298, 3371, 3444, 3520, 3597, 3676, 3756,
    3838, 3922, 4008];
  const QM4 = [0, -20456, -12896, -8968, -6288, -4240, -2584, -1200, 20456, 12896, 8968, 6288,
    4240, 2584, 1200, 0];
  const QM2 = [-7408, -1616, 7408, 1616];
  const IHN = [0, 1, 0];
  const IHP = [0, 3, 2];
  const WH = [0, -214, 798];
  const RH2 = [2, 1, 2, 1];
  const sat = (v) => (v > 32767 ? 32767 : v < -32768 ? -32768 : v);
  const band = (det) => ({
    s: 0, sp: 0, sz: 0, nb: 0, det,
    r: [0, 0, 0], a: [0, 0, 0], ap: [0, 0, 0], p: [0, 0, 0],
    d: [0, 0, 0, 0, 0, 0, 0], b: [0, 0, 0, 0, 0, 0, 0], bp: [0, 0, 0, 0, 0, 0, 0],
  });
  // Block 4: reconstruct, adapt the pole and zero predictors, predict.
  const update = (t, d) => {
    const { r, a, ap, p, b, bp } = t;
    const dd = t.d;
    dd[0] = d;
    r[0] = sat(t.s + d);
    p[0] = sat(t.sz + d);
    const sg0 = p[0] >> 15;
    const sg1 = p[1] >> 15;
    const sg2 = p[2] >> 15;
    const wd1 = sat(a[1] << 2);
    let wd2 = sg0 === sg1 ? -wd1 : wd1;
    if (wd2 > 32767) wd2 = 32767;
    const wd3 = (wd2 >> 7) + (sg0 === sg2 ? 128 : -128) + ((a[2] * 32512) >> 15);
    ap[2] = wd3 > 12288 ? 12288 : wd3 < -12288 ? -12288 : wd3;
    ap[1] = sat((sg0 === sg1 ? 192 : -192) + ((a[1] * 32640) >> 15));
    const limit = sat(15360 - ap[2]);
    if (ap[1] > limit) ap[1] = limit;
    else if (ap[1] < -limit) ap[1] = -limit;
    const step = d === 0 ? 0 : 128;
    const sgd = d >> 15;
    for (let i = 1; i < 7; i++) bp[i] = sat((dd[i] >> 15 === sgd ? step : -step) + ((b[i] * 32640) >> 15));
    for (let i = 6; i > 0; i--) {
      dd[i] = dd[i - 1];
      b[i] = bp[i];
    }
    for (let i = 2; i > 0; i--) {
      r[i] = r[i - 1];
      p[i] = p[i - 1];
      a[i] = ap[i];
    }
    t.sp = sat(((a[1] * sat(r[1] + r[1])) >> 15) + ((a[2] * sat(r[2] + r[2])) >> 15));
    let sz = 0;
    for (let i = 6; i > 0; i--) sz += (b[i] * sat(dd[i] + dd[i])) >> 15;
    t.sz = sat(sz);
    t.s = sat(t.sp + t.sz);
  };
  const x = new Int32Array(24);
  const low = band(32);
  const high = band(8);
  return (samples, out) => {
    let n = 0;
    for (let j = 0; j + 1 < samples.length; j += 2) {
      x.copyWithin(0, 2);
      x[22] = samples[j];
      x[23] = samples[j + 1];
      let sumodd = 0;
      let sumeven = 0;
      for (let i = 0; i < 12; i++) {
        sumodd += x[2 * i] * QMF[i];
        sumeven += x[2 * i + 1] * QMF[11 - i];
      }
      const xlow = (sumeven + sumodd) >> 14;
      const xhigh = (sumeven - sumodd) >> 14;

      const el = sat(xlow - low.s);
      let wd = el >= 0 ? el : -(el + 1);
      let i = 1;
      while (i < 30 && wd >= (Q6[i] * low.det) >> 12) i++;
      const ilow = el < 0 ? ILN[i] : ILP[i];
      const ril = ilow >> 2;
      const dlow = (low.det * QM4[ril]) >> 15;
      low.nb = ((low.nb * 127) >> 7) + WL[RL42[ril]];
      low.nb = low.nb < 0 ? 0 : low.nb > 18432 ? 18432 : low.nb;
      let shift = 8 - (low.nb >> 11);
      let base = ILB[(low.nb >> 6) & 31];
      low.det = (shift < 0 ? base << -shift : base >> shift) << 2;
      update(low, dlow);

      const eh = sat(xhigh - high.s);
      wd = eh >= 0 ? eh : -(eh + 1);
      const ihigh = (eh < 0 ? IHN : IHP)[wd >= (564 * high.det) >> 12 ? 2 : 1];
      const dhigh = (high.det * QM2[ihigh]) >> 15;
      high.nb = ((high.nb * 127) >> 7) + WH[RH2[ihigh]];
      high.nb = high.nb < 0 ? 0 : high.nb > 22528 ? 22528 : high.nb;
      shift = 10 - (high.nb >> 11);
      base = ILB[(high.nb >> 6) & 31];
      high.det = (shift < 0 ? base << -shift : base >> shift) << 2;
      update(high, dhigh);

      out[n++] = (ihigh << 6) | ilow;
    }
    return n;
  };
}

function createG711Encoder(codec) {
  const alaw = (pcm) => {
    let mask = 0xd5;
    let v = pcm >> 3;
    if (v < 0) {
      mask = 0x55;
      v = -v - 1;
    }
    const ends = [0x1f, 0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff];
    let seg = 0;
    while (seg < 8 && v > ends[seg]) seg++;
    if (seg >= 8) return 0x7f ^ mask;
    return ((seg << 4) | ((seg < 2 ? v >> 1 : v >> seg) & 0x0f)) ^ mask;
  };
  const ulaw = (pcm) => {
    let mask = 0xff;
    let v = pcm >> 2;
    if (v < 0) {
      v = -v;
      mask = 0x7f;
    }
    v = Math.min(v, 8159) + 0x21;
    const ends = [0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff, 0x1fff];
    let seg = 0;
    while (seg < 8 && v > ends[seg]) seg++;
    if (seg >= 8) return 0x7f ^ mask;
    return ((seg << 4) | ((v >> (seg + 1)) & 0x0f)) ^ mask;
  };
  const one = codec === "PCMU" ? ulaw : alaw;
  return (samples, out) => {
    for (let i = 0; i < samples.length; i++) out[i] = one(samples[i]);
    return samples.length;
  };
}

// Resamples the microphone to the codec rate, encodes 20 ms frames (160 bytes)
// and posts them. Muted, it keeps sending encoded silence so the DoorBell's
// G.722 decoder never loses step.
class AjaxDoorbellTalk extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const codec = (options.processorOptions && options.processorOptions.codec) || "G722";
    this.rate = codec === "G722" ? 16000 : 8000;
    this.step = sampleRate / this.rate;
    this.t = 0;
    this.prev = 0;
    this.frame = new Int16Array(codec === "G722" ? 320 : 160);
    this.fill = 0;
    this.encode = codec === "G722" ? createG722Encoder() : createG711Encoder(codec);
    this.live = false;
    this.stopped = false;
    this.port.onmessage = (event) => {
      const data = event.data || {};
      if (typeof data.live === "boolean") this.live = data.live;
      if (data.stop) this.stopped = true;
    };
  }

  push(value) {
    const v = value > 1 ? 1 : value < -1 ? -1 : value;
    this.frame[this.fill++] = this.live ? Math.round(v * 32767) : 0;
    if (this.fill === this.frame.length) {
      const bytes = new Uint8Array(160);
      this.encode(this.frame, bytes);
      this.port.postMessage(bytes.buffer, [bytes.buffer]);
      this.fill = 0;
    }
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0] && inputs[0][0];
    if (!input || !input.length) return true;
    const n = input.length;
    let t = this.t;
    // Linear interpolation; the card low-passes the input before any decimation.
    while (t < n - 1) {
      const i = Math.floor(t);
      const a = i < 0 ? this.prev : input[i];
      this.push(a + (input[i + 1] - a) * (t - i));
      t += this.step;
    }
    this.t = t - n;
    this.prev = input[n - 1];
    return true;
  }
}

registerProcessor("ajax-doorbell-talk", AjaxDoorbellTalk);
`;

const ICONS = {
  answer: "mdi:phone",
  listen: "mdi:ear-hearing",
  hangup: "mdi:phone-hangup",
  view: "mdi:eye",
  close: "mdi:close",
  ignore: "mdi:bell-off",
  bell: "mdi:bell-ring",
  inCall: "mdi:phone-in-talk",
  mic: "mdi:microphone",
  micOff: "mdi:microphone-off",
  volume: "mdi:volume-high",
  volumeOff: "mdi:volume-off",
  fullscreen: "mdi:fullscreen",
  fullscreenExit: "mdi:fullscreen-exit",
  retry: "mdi:refresh",
  alert: "mdi:alert-circle-outline",
  doorbell: "mdi:doorbell-video",
};

const STRINGS = {
  fr: {
    view: "Voir",
    answer: "Décrocher",
    listen: "Écouter",
    ignore: "Ignorer",
    hang_up: "Raccrocher",
    close: "Fermer",
    retry: "Réessayer",
    ringing: "On sonne à la porte",
    answered_on: "Décroché sur {who}",
    call_in_progress: "Appel en cours",
    connecting: "Connexion…",
    reconnecting: "Reconnexion…",
    live: "En direct",
    unavailable: "Indisponible",
    unmute: "Activer le son",
    mic: "Micro",
    mic_mute: "Couper le micro",
    mic_unmute: "Activer le micro",
    speaker: "Son",
    speaker_mute: "Couper le son",
    speaker_unmute: "Activer le son",
    fullscreen: "Plein écran",
    exit_fullscreen: "Quitter le plein écran",
    ptt: "Maintenir pour parler",
    ptt_active: "Parlez…",
    ptt_hint: "Maintenez le bouton (ou la barre d'espace) pour parler",
    call_time: "Durée de l'appel",
    call_ended: "Appel terminé",
    taken_over: "Appel repris sur {who}",
    mic_insecure:
      "Micro indisponible : ouvrez Home Assistant en HTTPS pour parler. Vous voyez et entendez quand même.",
    mic_denied:
      "Micro refusé : autorisez-le pour ce site dans le navigateur, puis touchez le bouton micro.",
    mic_missing: "Aucun micro détecté sur cet appareil.",
    mic_busy: "Le micro est utilisé par une autre application.",
    mic_unsupported: "Ce navigateur ne donne pas accès au micro.",
    mic_failed: "Micro indisponible ({name}).",
    mic_taken: "Le micro a été repris sur un autre écran.",
    mic_ended: "La sonnette a coupé le micro.",
    mic_relay: "La sonnette refuse le micro : {message}",
    err_ice: "Impossible de joindre la sonnette (réseau).",
    err_lost: "Connexion perdue.",
    err_timeout: "La sonnette ne répond pas.",
    err_webrtc: "Cette caméra ne propose pas de flux WebRTC dans Home Assistant.",
    err_signal: "Erreur du flux : {message}",
    err_generic: "Erreur : {message}",
    not_found: "Entité introuvable : {entity}",
    default_name: "Sonnette",
    phone: "téléphone",
    tablet: "tablette",
    computer: "ordinateur",
    st_rtt: "RTT",
    st_jitter: "Gigue",
    st_loss: "Pertes",
    st_video: "Vidéo",
    st_audio_in: "Son reçu",
    st_audio_out: "Micro",
    st_off: "coupé",
  },
  en: {
    view: "View",
    answer: "Answer",
    listen: "Listen",
    ignore: "Ignore",
    hang_up: "Hang up",
    close: "Close",
    retry: "Retry",
    ringing: "Someone is at the door",
    answered_on: "Answered on {who}",
    call_in_progress: "Call in progress",
    connecting: "Connecting…",
    reconnecting: "Reconnecting…",
    live: "Live",
    unavailable: "Unavailable",
    unmute: "Turn sound on",
    mic: "Microphone",
    mic_mute: "Mute the microphone",
    mic_unmute: "Turn the microphone on",
    speaker: "Sound",
    speaker_mute: "Mute the sound",
    speaker_unmute: "Turn sound on",
    fullscreen: "Full screen",
    exit_fullscreen: "Exit full screen",
    ptt: "Hold to talk",
    ptt_active: "Talking…",
    ptt_hint: "Hold the button (or the space bar) to talk",
    call_time: "Call duration",
    call_ended: "Call ended",
    taken_over: "Call picked up on {who}",
    mic_insecure:
      "Microphone unavailable: open Home Assistant over HTTPS to talk. You still see and hear.",
    mic_denied:
      "Microphone blocked: allow it for this site in your browser, then tap the microphone button.",
    mic_missing: "No microphone found on this device.",
    mic_busy: "The microphone is used by another app.",
    mic_unsupported: "This browser gives no access to the microphone.",
    mic_failed: "Microphone unavailable ({name}).",
    mic_taken: "Another screen took the microphone.",
    mic_ended: "The doorbell closed the microphone.",
    mic_relay: "The doorbell refused the microphone: {message}",
    err_ice: "Can't reach the doorbell (network).",
    err_lost: "Connection lost.",
    err_timeout: "The doorbell is not answering.",
    err_webrtc: "This camera has no WebRTC stream in Home Assistant.",
    err_signal: "Stream error: {message}",
    err_generic: "Error: {message}",
    not_found: "Entity not found: {entity}",
    default_name: "Doorbell",
    phone: "phone",
    tablet: "tablet",
    computer: "computer",
    st_rtt: "RTT",
    st_jitter: "Jitter",
    st_loss: "Loss",
    st_video: "Video",
    st_audio_in: "Audio in",
    st_audio_out: "Mic",
    st_off: "off",
  },
};

const EMPTY_CALL = Object.freeze({
  entity: null,
  state: null,
  ringStarted: null,
  answeredBy: null,
  answeredAt: null,
  twoWay: undefined,
  talkVia: null,
});

const debug = (...args) => console.debug("[ajax-doorbell-card]", ...args);

/**
 * Talk-back through the integration: the DoorBell only takes G.722, which
 * go2rtc cannot make from a browser, so the microphone is encoded here and
 * sent as 20 ms binary frames on the Home Assistant websocket.
 */
class TalkRelay {
  constructor(hass, entityId, name, onEnded) {
    this._hass = hass;
    this._entityId = entityId;
    this._name = name;
    this._onEnded = onEnded;
    this._live = false;
    this._closed = false;
    this.ready = false;
    this.codec = null;
  }

  async start(track) {
    let resolveReady;
    let rejectReady;
    const ready = new Promise((resolve, reject) => {
      resolveReady = resolve;
      rejectReady = reject;
    });
    const timer = setTimeout(() => rejectReady(new Error("timeout")), TALK_READY_MS);
    try {
      const unsub = await this._hass.connection.subscribeMessage(
        (event) => {
          if (event.type === "ready") {
            this._handler = event.handler_id; // also after a websocket reconnect
            resolveReady(event);
          } else if (event.type === "ended") {
            this._end(event.reason);
          }
        },
        { type: `${DOMAIN}/talk`, entity_id: this._entityId, name: this._name },
      );
      this._unsub = unsub;
      if (this._closed) return this.stop();
      const info = await ready;
      if (this._closed) return undefined;
      this.codec = info.codec;
      await this._startAudio(track, info.codec, info.sample_rate);
      if (this._closed) return this.stop();
      this.ready = true;
      this._node.port.postMessage({ live: this._live });
      return undefined;
    } finally {
      clearTimeout(timer);
    }
  }

  async _startAudio(track, codec, rate) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const stream = new MediaStream([track]);
    let ctx = null;
    let source = null;
    try {
      ctx = new Ctx({ sampleRate: rate, latencyHint: "interactive" });
      source = ctx.createMediaStreamSource(stream);
    } catch (err) {
      // Firefox refuses a microphone in a context at another rate: resample here.
      if (ctx) ctx.close().catch(() => {});
      ctx = new Ctx({ latencyHint: "interactive" });
      source = ctx.createMediaStreamSource(stream);
    }
    this._ctx = ctx;
    this._source = source;
    const url = URL.createObjectURL(new Blob([TALK_WORKLET], { type: "text/javascript" }));
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    if (this._closed) return;
    const node = new AudioWorkletNode(ctx, "ajax-doorbell-talk", { processorOptions: { codec } });
    node.port.onmessage = (event) => this._send(event.data);
    this._node = node;
    let head = source;
    if (ctx.sampleRate > rate) {
      // A 4th-order Butterworth low-pass below the new Nyquist frequency.
      for (const q of [0.5412, 1.3066]) {
        const filter = ctx.createBiquadFilter();
        filter.type = "lowpass";
        filter.frequency.value = rate * 0.45;
        filter.Q.value = q;
        head.connect(filter);
        head = filter;
      }
    }
    head.connect(node);
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
  }

  /** Speak (true) or send silence (false); the session stays open. */
  setLive(live) {
    this._live = live;
    if (this._node) this._node.port.postMessage({ live });
  }

  _send(buffer) {
    if (!this.ready || this._closed || !this._handler) return;
    const socket = this._hass.connection && this._hass.connection.socket;
    if (!socket || socket.readyState !== 1) return;
    const message = new Uint8Array(buffer.byteLength + 1);
    message[0] = this._handler;
    message.set(new Uint8Array(buffer), 1);
    socket.send(message);
  }

  stop() {
    this._closed = true;
    this.ready = false;
    const unsub = this._unsub;
    this._unsub = null;
    if (unsub) Promise.resolve().then(unsub).catch(() => {});
    if (this._node) {
      this._node.port.onmessage = null;
      this._node.port.postMessage({ stop: true });
      try {
        this._node.disconnect();
      } catch (err) {
        /* ignore */
      }
      this._node = null;
    }
    if (this._source) {
      try {
        this._source.disconnect();
      } catch (err) {
        /* ignore */
      }
      this._source = null;
    }
    if (this._ctx) {
      this._ctx.close().catch(() => {});
      this._ctx = null;
    }
  }

  _end(reason) {
    if (this._closed) return;
    this.stop();
    this._onEnded(reason);
  }
}

let deviceKind = null;

/** Phone, tablet or computer, for the default "answered by" label. */
function guessDevice() {
  if (deviceKind) return deviceKind;
  const nav = typeof navigator !== "undefined" ? navigator : {};
  const ua = nav.userAgent || "";
  const touch = (nav.maxTouchPoints || 0) > 0;
  const scr = typeof screen !== "undefined" ? screen : null;
  const shortSide = scr ? Math.min(scr.width || 0, scr.height || 0) : 0;
  if (
    /iPad|Tablet|PlayBook|Silk|Kindle/i.test(ua) ||
    (/Macintosh/i.test(ua) && touch) || // iPadOS presents itself as a Mac
    (/Android/i.test(ua) && !/Mobile/i.test(ua))
  ) {
    deviceKind = "tablet";
  } else if (/Mobi|iPhone|iPod|Android|Windows Phone/i.test(ua)) {
    deviceKind = "phone";
  } else if (touch && shortSide && shortSide < 600) {
    deviceKind = "phone";
  } else {
    deviceKind = "computer";
  }
  return deviceKind;
}

/** "16 / 9", "16:9", "4x3", "56.25%" or 1.78 -> a CSS aspect-ratio value. */
function normalizeAspect(value) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value === "number") return value > 0 ? String(value) : null;
  const text = String(value).trim();
  const pair = text.match(/^(\d+(?:\.\d+)?)\s*[:/x×]\s*(\d+(?:\.\d+)?)$/i);
  if (pair && Number(pair[1]) > 0 && Number(pair[2]) > 0) return `${pair[1]} / ${pair[2]}`;
  const percent = text.match(/^(\d+(?:\.\d+)?)\s*%$/);
  if (percent && Number(percent[1]) > 0) return `100 / ${percent[1]}`;
  const number = Number(text);
  return number > 0 ? String(number) : null;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function setAttr(el, name, value) {
  if (el && el.getAttribute(name) !== value) el.setAttribute(name, value);
}

function setClass(el, value) {
  if (el && el.className !== value) el.className = value;
}

const STYLE = `
  :host {
    display: block;
    --adb-green: var(--ajax-doorbell-answer-color, var(--success-color, #2fb344));
    --adb-red: var(--ajax-doorbell-hangup-color, var(--error-color, #e5484d));
    --adb-neutral: rgba(var(--rgb-primary-text-color, 128, 128, 128), 0.1);
    --adb-neutral-hover: rgba(var(--rgb-primary-text-color, 128, 128, 128), 0.17);
  }
  ha-card {
    position: relative;
    overflow: hidden;
    height: 100%;
    box-sizing: border-box;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    color: var(--primary-text-color, #212121);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, none);
  }
  .inner {
    position: relative;
    display: flex;
    flex-direction: column;
    height: 100%;
    container-type: inline-size;
  }
  .inner.ringing { min-height: 290px; }

  /* ---------- stage: poster, video and what floats over them ---------- */
  .stage {
    position: relative;
    /* May shrink when the card gets a fixed height (panel view, grid rows): the
       controls stay visible and the video letterboxes instead. */
    flex: 0 1 auto;
    min-height: 0;
    width: 100%;
    aspect-ratio: var(--adb-aspect, 16 / 9);
    background: #000;
    overflow: hidden;
    isolation: isolate;
  }
  .placeholder {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    color: rgba(255, 255, 255, 0.28);
    background: radial-gradient(circle at 50% 40%, #2b2f36, #0d0f12 70%);
  }
  .placeholder ha-icon { --mdc-icon-size: 64px; }
  .stage.has-poster .placeholder,
  .stage.has-video .placeholder { display: none; }
  .poster,
  video {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }
  .poster { opacity: 0; transition: opacity 0.3s ease; }
  .stage.has-poster .poster { opacity: 1; }
  video { opacity: 0; transition: opacity 0.35s ease; background: transparent; }
  .stage.has-video video { opacity: 1; }
  .stage.has-video .poster { opacity: 0; }
  .shade {
    position: absolute;
    inset: 0 0 auto 0;
    height: 84px;
    z-index: 1;
    pointer-events: none;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.55), rgba(0, 0, 0, 0));
  }
  .topbar {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    color: #fff;
    pointer-events: none;
  }
  .name {
    min-width: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.6);
  }
  .pills { display: flex; gap: 6px; flex: 0 0 auto; }
  .pill {
    display: none;
    align-items: center;
    gap: 6px;
    height: 24px;
    padding: 0 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    color: #fff;
    background: rgba(0, 0, 0, 0.45);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
  }
  .pill.show { display: inline-flex; }
  .pill ha-icon { --mdc-icon-size: 15px; }
  .badge .dot { width: 8px; height: 8px; border-radius: 50%; background: #9e9e9e; }
  .badge.live { font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
  .badge.live .dot { background: #ff3b30; animation: live 1.6s ease-out infinite; }
  .badge.reconnecting .dot { background: #ffb300; animation: breathe 1s ease-in-out infinite; }
  .timer { font-variant-numeric: tabular-nums; }
  .banner {
    position: absolute;
    top: 44px;
    left: 50%;
    z-index: 2;
    transform: translateX(-50%);
    display: none;
    align-items: center;
    gap: 8px;
    max-width: calc(100% - 24px);
    box-sizing: border-box;
    padding: 8px 14px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    color: #fff;
    background: rgba(0, 0, 0, 0.6);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
  }
  .banner.show { display: inline-flex; }
  .banner.ring { background: rgba(245, 124, 0, 0.92); animation: breathe-soft 1.4s ease-in-out infinite; }
  .banner ha-icon { --mdc-icon-size: 18px; flex: 0 0 auto; }
  .banner span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .center {
    position: absolute;
    inset: 0;
    z-index: 3;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 16px;
    text-align: center;
    color: #fff;
    background: rgba(0, 0, 0, 0.38);
  }
  .center.show { display: flex; }
  .center .msg {
    max-width: 360px;
    font-size: 14px;
    font-weight: 500;
    line-height: 1.4;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
  }
  .center ha-icon.big { --mdc-icon-size: 40px; opacity: 0.9; }
  .spinner {
    width: 38px;
    height: 38px;
    border-radius: 50%;
    border: 3px solid rgba(255, 255, 255, 0.25);
    border-top-color: #fff;
    animation: spin 0.9s linear infinite;
  }
  .unmute {
    position: absolute;
    left: 50%;
    bottom: 14px;
    z-index: 4;
    transform: translateX(-50%);
    display: none;
    align-items: center;
    gap: 8px;
    min-height: 48px;
    padding: 0 20px;
    border: 0;
    border-radius: 999px;
    font: inherit;
    font-size: 14px;
    font-weight: 600;
    color: #fff;
    background: rgba(0, 0, 0, 0.68);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
    cursor: pointer;
  }
  .unmute.show { display: inline-flex; }
  .toast {
    position: absolute;
    top: 50%;
    left: 50%;
    z-index: 4;
    transform: translate(-50%, -50%);
    max-width: calc(100% - 32px);
    box-sizing: border-box;
    padding: 10px 16px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 500;
    text-align: center;
    color: #fff;
    background: rgba(0, 0, 0, 0.74);
    opacity: 0;
    transition: opacity 0.25s ease;
    pointer-events: none;
  }
  .toast.show { opacity: 1; }
  .stats {
    position: absolute;
    left: 8px;
    bottom: 8px;
    z-index: 3;
    display: none;
    max-width: calc(100% - 16px);
    overflow: hidden;
    padding: 6px 9px;
    border-radius: 8px;
    color: #d7ffd9;
    background: rgba(0, 0, 0, 0.6);
    font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre;
    pointer-events: none;
  }
  .stats.show { display: block; }

  /* ---------- controls ---------- */
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 12px;
  }
  .controls:empty { display: none; }
  .btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-width: 48px;
    min-height: 48px;
    padding: 0 20px;
    border: 0;
    border-radius: 999px;
    font: inherit;
    font-size: 15px;
    font-weight: 600;
    line-height: 1;
    color: var(--primary-text-color, #212121);
    background: var(--adb-neutral);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none;
    user-select: none;
    transition: background-color 0.2s ease, color 0.2s ease, box-shadow 0.15s ease, transform 0.12s ease;
  }
  .btn ha-icon { --mdc-icon-size: 22px; flex: 0 0 auto; }
  .btn:active { transform: scale(0.96); }
  .btn.wide { flex: 1 1 150px; max-width: 320px; }
  .btn.round { flex: 0 0 auto; width: 52px; height: 52px; padding: 0; }
  .btn.primary { color: var(--text-primary-color, #fff); background: var(--primary-color, #03a9f4); }
  .btn.answer { color: #fff; background: var(--adb-green); box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); }
  .btn.danger { color: #fff; background: var(--adb-red); box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); }
  .btn.danger-soft { color: var(--adb-red); }
  .btn.on { color: var(--text-primary-color, #fff); background: var(--primary-color, #03a9f4); }
  .btn.off { color: var(--adb-red); }
  .btn.warn { box-shadow: inset 0 0 0 2px var(--warning-color, #ffa600); }
  .btn.busy ha-icon { animation: breathe 1s ease-in-out infinite; }
  .btn.round.on { box-shadow: 0 0 0 calc(var(--lvl, 0) * 10px) rgba(var(--rgb-primary-color, 3, 169, 244), 0.35); }
  .btn.ptt { touch-action: none; -webkit-touch-callout: none; }
  .btn.ptt.active {
    color: #fff;
    background: var(--adb-green);
    box-shadow: 0 0 0 calc(3px + var(--lvl, 0) * 12px) rgba(47, 179, 68, 0.3);
  }
  .btn:focus-visible,
  .rbtn:focus-visible,
  .unmute:focus-visible {
    outline: 3px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  @media (hover: hover) {
    .btn:hover { filter: brightness(1.06); }
    .btn:not(.on):not(.answer):not(.danger):not(.primary):not(.active):hover { background: var(--adb-neutral-hover); }
  }
  .hint {
    display: none;
    align-items: flex-start;
    gap: 8px;
    padding: 0 16px 14px;
    font-size: 13px;
    line-height: 1.4;
    color: var(--secondary-text-color, #727272);
  }
  .hint.show { display: flex; }
  .hint ha-icon { --mdc-icon-size: 18px; flex: 0 0 auto; color: var(--warning-color, #ffa600); }

  /* ---------- incoming call ---------- */
  .ring {
    position: absolute;
    inset: 0;
    z-index: 6;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 18px 16px 20px;
    text-align: center;
    color: #fff;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.4), rgba(0, 0, 0, 0.22) 42%, rgba(0, 0, 0, 0.8));
    -webkit-backdrop-filter: blur(2px);
    backdrop-filter: blur(2px);
  }
  .ring.show { display: flex; animation: fade-in 0.25s ease; }
  .ring-head { display: flex; flex-direction: column; align-items: center; gap: 8px; padding-top: 4px; }
  .bell {
    position: relative;
    display: grid;
    place-items: center;
    width: 72px;
    height: 72px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.16);
    -webkit-backdrop-filter: blur(6px);
    backdrop-filter: blur(6px);
  }
  .bell ha-icon { --mdc-icon-size: 38px; transform-origin: 50% 12%; animation: shake 1.6s ease-in-out infinite; }
  .bell .wave {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.6);
    animation: wave 2s ease-out infinite;
  }
  .bell .wave.w2 { animation-delay: 1s; }
  .ring-title { font-size: 22px; font-weight: 700; letter-spacing: 0.01em; text-shadow: 0 2px 6px rgba(0, 0, 0, 0.5); }
  .ring-sub { font-size: 14px; opacity: 0.88; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5); }
  .ring-actions { display: flex; align-items: flex-end; justify-content: center; gap: 28px; width: 100%; }
  .rbtn {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    min-width: 64px;
    padding: 0;
    border: 0;
    border-radius: 16px;
    font: inherit;
    font-size: 14px;
    font-weight: 600;
    color: #fff;
    background: none;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .rbtn .disc {
    position: relative;
    display: grid;
    place-items: center;
    width: 60px;
    height: 60px;
    border-radius: 50%;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.35);
    transition: transform 0.12s ease;
  }
  .rbtn .disc ha-icon { --mdc-icon-size: 28px; }
  .rbtn:active .disc { transform: scale(0.93); }
  .rbtn.ignore .disc { background: var(--adb-red); }
  .rbtn.view .disc {
    background: rgba(255, 255, 255, 0.22);
    -webkit-backdrop-filter: blur(8px);
    backdrop-filter: blur(8px);
  }
  .rbtn.answer .disc { width: 76px; height: 76px; background: var(--adb-green); }
  .rbtn.answer .disc ha-icon { --mdc-icon-size: 34px; animation: wiggle 1.6s ease-in-out infinite; }
  .rbtn.answer .disc::after {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 3px solid var(--adb-green);
    animation: wave 1.8s ease-out infinite;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    clip-path: inset(50%);
    white-space: nowrap;
  }

  /* ---------- full screen (real, or in-page when the browser refuses) ---------- */
  ha-card.fs { border-radius: 0; color: #fff; background: #000; }
  ha-card.pseudo-fs {
    position: fixed;
    inset: 0;
    z-index: 9999;
    width: auto;
    height: auto;
    margin: 0;
    border-width: 0;
    box-shadow: none;
    overscroll-behavior: contain;
  }
  ha-card.fs .stage { flex: 1 1 auto; min-height: 0; aspect-ratio: auto; }
  ha-card.fs .topbar { padding-top: max(10px, env(safe-area-inset-top)); }
  ha-card.fs .controls { padding-bottom: max(12px, env(safe-area-inset-bottom)); }
  ha-card.fs .btn:not(.on):not(.answer):not(.danger):not(.primary):not(.active) {
    color: #fff;
    background: rgba(255, 255, 255, 0.14);
  }
  ha-card.fs .btn.off,
  ha-card.fs .btn.danger-soft { color: #ff8a8a; }
  ha-card.fs .hint { color: rgba(255, 255, 255, 0.75); }

  /* ---------- sizes ---------- */
  @container (max-width: 440px) {
    .ring { padding: 12px 10px 14px; }
    .bell { width: 54px; height: 54px; }
    .bell ha-icon { --mdc-icon-size: 30px; }
    .ring-title { font-size: 18px; }
    .ring-sub { font-size: 13px; }
    .ring-actions { gap: 18px; }
    .rbtn .disc { width: 52px; height: 52px; }
    .rbtn .disc ha-icon { --mdc-icon-size: 24px; }
    .rbtn.answer .disc { width: 62px; height: 62px; }
    .rbtn.answer .disc ha-icon { --mdc-icon-size: 28px; }
    .controls { gap: 8px; padding: 10px; }
    .btn { padding: 0 16px; font-size: 14px; }
    .banner { top: 40px; font-size: 12px; }
  }
  @container (max-width: 330px) {
    .btn.wide { flex-basis: 100%; }
  }
  @container (min-width: 700px) {
    .bell { width: 96px; height: 96px; }
    .bell ha-icon { --mdc-icon-size: 50px; }
    .ring-title { font-size: 30px; }
    .ring-sub { font-size: 17px; }
    .ring-actions { gap: 56px; }
    .rbtn { font-size: 16px; }
    .rbtn .disc { width: 74px; height: 74px; }
    .rbtn .disc ha-icon { --mdc-icon-size: 34px; }
    .rbtn.answer .disc { width: 94px; height: 94px; }
    .rbtn.answer .disc ha-icon { --mdc-icon-size: 42px; }
    .name { font-size: 17px; }
  }

  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes wave {
    from { transform: scale(1); opacity: 0.85; }
    to { transform: scale(1.75); opacity: 0; }
  }
  @keyframes shake {
    0%, 52%, 100% { transform: rotate(0); }
    4%, 12%, 20%, 28%, 36%, 44% { transform: rotate(13deg); }
    8%, 16%, 24%, 32%, 40%, 48% { transform: rotate(-13deg); }
  }
  @keyframes wiggle {
    0%, 60%, 100% { transform: rotate(0); }
    10%, 30%, 50% { transform: rotate(-12deg); }
    20%, 40% { transform: rotate(12deg); }
  }
  @keyframes live {
    0% { box-shadow: 0 0 0 0 rgba(255, 59, 48, 0.7); }
    70% { box-shadow: 0 0 0 7px rgba(255, 59, 48, 0); }
    100% { box-shadow: 0 0 0 0 rgba(255, 59, 48, 0); }
  }
  @keyframes breathe { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  @keyframes breathe-soft { 0%, 100% { opacity: 1; } 50% { opacity: 0.8; } }
  @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition: none !important;
    }
  }
`;

const TEMPLATE = `
  <ha-card id="card">
    <div class="inner" id="inner">
      <div class="stage" id="stage">
        <div class="placeholder" aria-hidden="true"><ha-icon icon="${ICONS.doorbell}"></ha-icon></div>
        <img class="poster" id="poster" alt="" decoding="async" draggable="false" />
        <video id="video" playsinline autoplay muted></video>
        <div class="shade" aria-hidden="true"></div>
        <div class="topbar">
          <span class="name" id="name"></span>
          <span class="pills">
            <span class="pill badge" id="badge"><span class="dot"></span><span id="badge-text"></span></span>
            <span class="pill timer" id="timer" role="timer"
              ><ha-icon icon="${ICONS.inCall}"></ha-icon><span id="timer-text"></span
            ></span>
          </span>
        </div>
        <div class="banner" id="banner" role="status"
          ><ha-icon id="banner-icon" icon="${ICONS.inCall}"></ha-icon><span id="banner-text"></span
        ></div>
        <div class="center" id="center"></div>
        <button class="unmute" id="unmute" type="button"
          ><ha-icon icon="${ICONS.volume}"></ha-icon><span id="unmute-text"></span
        ></button>
        <div class="toast" id="toast" role="status" aria-live="polite"></div>
        <div class="stats" id="stats" aria-hidden="true"></div>
      </div>
      <div class="controls" id="controls"></div>
      <div class="hint" id="hint" role="status"
        ><ha-icon icon="${ICONS.micOff}"></ha-icon><span id="hint-text"></span
      ></div>
      <div class="ring" id="ring" role="dialog" aria-modal="false" aria-labelledby="ring-title" aria-describedby="ring-sub">
        <div class="ring-head">
          <div class="bell" aria-hidden="true">
            <span class="wave"></span><span class="wave w2"></span><ha-icon icon="${ICONS.bell}"></ha-icon>
          </div>
          <div class="ring-title" id="ring-title"></div>
          <div class="ring-sub" id="ring-sub"></div>
        </div>
        <div class="ring-actions">
          <button class="rbtn ignore" id="ring-ignore" type="button"
            ><span class="disc"><ha-icon icon="${ICONS.ignore}"></ha-icon></span
            ><span id="ring-ignore-label"></span
          ></button>
          <button class="rbtn view" id="ring-view" type="button"
            ><span class="disc"><ha-icon icon="${ICONS.view}"></ha-icon></span
            ><span id="ring-view-label"></span
          ></button>
          <button class="rbtn answer" id="ring-answer" type="button"
            ><span class="disc"><ha-icon id="ring-answer-icon" icon="${ICONS.answer}"></ha-icon></span
            ><span id="ring-answer-label"></span
          ></button>
        </div>
      </div>
      <div class="sr-only" id="announcer" aria-live="assertive"></div>
    </div>
  </ha-card>
`;

class AjaxDoorbellCard extends HTMLElement {
  /* ---------- Lovelace API ---------- */

  static getStubConfig(hass) {
    const entities = (hass && hass.entities) || {};
    const states = (hass && hass.states) || {};
    const cameras = Object.keys(states).filter((id) => id.startsWith("camera."));
    const ajax = cameras.filter((id) => entities[id] && entities[id].platform === DOMAIN);
    const main = ajax.find((id) => entities[id].translation_key !== "substream");
    return { camera: main || ajax[0] || cameras[0] || "camera.ajax_doorbell" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._isPreview = false;
    this._built = false;
    this._els = {};
    this._ctrl = {};
    this._controlsKey = null;
    this._centerKey = null;
    this._timers = new Map();
    this._listening = false;
    this._io = null;
    this._visible = true;

    // Media session: one RTCPeerConnection, renewed only on failure or "Fermer".
    this._gen = 0;
    this._pc = null;
    this._audioTx = null;
    this._unsub = null;
    this._sessionId = null;
    this._localCandidates = [];
    this._remoteCandidates = [];
    this._remoteReady = false;
    this._remoteStream = null;
    this._media = "idle"; // idle | connecting | live | error
    this._mode = "none"; // none | preview | view | talk
    this._reconnecting = false;
    this._videoReady = false;
    this._errorText = "";
    this._retries = 0;
    this._speakerOn = true;
    this._soundBlocked = false;
    this._statsPrev = null;

    // Microphone.
    this._micTrack = null;
    this._micWanted = false;
    this._micBusy = false;
    this._micError = null;
    this._micReq = 0;
    this._micLive = false;
    this._sentTrack = null;
    this._pttDown = false;
    this._meter = null;

    // Call, as seen by the integration's call state sensor.
    this._call = EMPTY_CALL;
    this._callSeen = EMPTY_CALL;
    this._ownCall = false;
    this._callConfirmed = false;
    this._callStart = 0;
    this._myLabel = null;
    this._pendingAnswer = null;
    this._ringAck = null;
    this._ceCache = null;
    this._caps = undefined;
    this._capsFor = null;
    this._capsPromise = null;

    // Poster, full screen, sounds, wake lock.
    this._posterLoaded = false;
    this._posterReq = 0;
    this._aspectForced = false;
    this._aspectFromVideo = false;
    this._fsReal = false;
    this._fsPseudo = false;
    this._fsAuto = false;
    this._audioCtx = null;
    this._ringtone = null;
    this._wakeLock = null;
    this._wakeLockPending = false;

    this._onVisibility = () => this._handleVisibility();
    this._onFsChange = () => this._handleFsChange();
    this._onLocation = () => this._checkAutoAnswer();
    this._onKey = (event) => {
      if (event.key === "Escape" && this._fsPseudo) this._exitFullscreen();
    };
    this._onBlur = () => this._pttEnd();
    this._onPageHide = () => {
      if (this._ownCall) this._hangUp();
    };
  }

  setConfig(config) {
    if (!config || typeof config.camera !== "string" || !config.camera.startsWith("camera.")) {
      throw new Error("ajax-doorbell-card: set 'camera' to a camera entity (camera.xxx)");
    }
    const previous = this._config;
    if (previous && (previous.camera !== config.camera || previous.call_entity !== config.call_entity)) {
      // Another doorbell: end what runs on the old one while the old config is still in place.
      if (this._ownCall) this._hangUp();
      else this._teardown();
      this._call = EMPTY_CALL;
      this._callSeen = EMPTY_CALL;
      this._posterLoaded = false;
    }
    this._config = {
      preview_on_ring: true,
      ring_sound: false,
      fullscreen_on_ring: false,
      push_to_talk: false,
      show_stats: false,
      auto_retry: true,
      ...config,
    };
    this._ceCache = null;
    this._caps = undefined;
    this._capsFor = null;
    this._controlsKey = null;
    this._centerKey = null;
    if (this._built) {
      this._applyAspect();
      this._update();
      this._refreshPoster(true);
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (!this._built) this._build();
    this._update();
    const search = (typeof window !== "undefined" && window.location && window.location.search) || "";
    if (search.indexOf("ajax_answer") !== -1) this._checkAutoAnswer();
  }

  get hass() {
    return this._hass;
  }

  /* Set by Home Assistant on card previews (picker, editor): no ringing side effects there. */
  set preview(value) {
    this._isPreview = Boolean(value);
  }

  get preview() {
    return this._isPreview;
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  connectedCallback() {
    this._clearTimer("detach");
    this._listen(true);
    this._setInterval("poster", () => this._refreshPoster(false), POSTER_REFRESH_MS);
    if (this._built) {
      this._update();
      this._refreshPoster(!this._posterLoaded);
      const video = this._els.video;
      if (this._pc && video && video.srcObject && video.paused) this._applyAudio();
    }
    this._checkAutoAnswer();
  }

  disconnectedCallback() {
    this._listen(false);
    this._clearTimer("poster");
    this._pttEnd();
    // Lovelace sometimes detaches a card only to re-attach it (column reflow):
    // clean up only if it stays detached.
    this._setTimeout("detach", () => this._onDetached(), DETACH_GRACE_MS);
  }

  _onDetached() {
    if (this.isConnected) return;
    debug("card removed: cleaning up");
    if (this._ownCall) this._hangUp();
    else this._teardown();
    this._stopRingtone();
    this._clearAllTimers();
    if (this._audioCtx) {
      const ctx = this._audioCtx;
      this._audioCtx = null;
      Promise.resolve()
        .then(() => ctx.close())
        .catch(() => {});
    }
    this._callSeen = EMPTY_CALL;
  }

  _listen(on) {
    if (on === this._listening) return;
    this._listening = on;
    const method = on ? "addEventListener" : "removeEventListener";
    document[method]("visibilitychange", this._onVisibility);
    document[method]("fullscreenchange", this._onFsChange);
    document[method]("webkitfullscreenchange", this._onFsChange);
    window[method]("location-changed", this._onLocation);
    window[method]("popstate", this._onLocation);
    window[method]("keydown", this._onKey);
    window[method]("blur", this._onBlur);
    window[method]("pagehide", this._onPageHide);
    if (on && typeof IntersectionObserver === "function") {
      this._io = new IntersectionObserver((entries) => {
        const visible = entries.some((entry) => entry.isIntersecting);
        if (visible === this._visible) return;
        this._visible = visible;
        if (visible) this._refreshPoster(false);
      });
      this._io.observe(this);
    } else if (!on && this._io) {
      this._io.disconnect();
      this._io = null;
    }
  }

  /* ---------- entities ---------- */

  _stateObj() {
    return (this._hass && this._config && this._hass.states[this._config.camera]) || null;
  }

  /** The integration's call state sensor: config override, else the camera device's. */
  _callEntityId() {
    if (!this._config) return null;
    if (this._config.call_entity) return this._config.call_entity;
    const hass = this._hass;
    if (!hass || !hass.entities) return null;
    const cache = this._ceCache;
    if (cache && cache.ref === hass.entities && cache.camera === this._config.camera) return cache.id;
    const camera = hass.entities[this._config.camera];
    const device = camera && camera.device_id;
    let id = null;
    if (device) {
      const found = Object.values(hass.entities).find(
        (entry) =>
          entry.device_id === device && entry.platform === DOMAIN && entry.translation_key === "call_state"
      );
      id = found ? found.entity_id : null;
    }
    this._ceCache = { ref: hass.entities, camera: this._config.camera, id };
    return id;
  }

  _readCall() {
    const id = this._callEntityId();
    const st = id && this._hass && this._hass.states[id];
    if (!st) return EMPTY_CALL;
    const attrs = st.attributes || {};
    return {
      entity: id,
      state: st.state,
      ringStarted: attrs.ring_started || null,
      answeredBy: attrs.answered_by || null,
      answeredAt: attrs.answered_at || null,
      twoWay: typeof attrs.two_way_audio === "boolean" ? attrs.two_way_audio : undefined,
      talkVia: attrs.talk_via || null,
    };
  }

  _ringKey() {
    return this._call.ringStarted || "ring";
  }

  /** The integration relays the voice itself (G.722 doorbells): WebRTC only receives. */
  _relayMode() {
    return this._call.talkVia === "relay";
  }

  /** Talk-back: forced by the config, else the call sensor, else HA's camera capabilities. */
  _canTalk() {
    const forced = this._config && this._config.two_way_audio;
    if (forced === true || forced === false) return forced;
    if (this._call.entity && this._call.twoWay !== undefined) return this._call.twoWay;
    return Boolean(this._caps && this._capsFor === this._config.camera && this._caps.two_way_audio);
  }

  _needsCaps() {
    const forced = this._config.two_way_audio;
    if (forced === true || forced === false) return false;
    if (this._call.entity && this._call.twoWay !== undefined) return false;
    return this._capsFor !== this._config.camera;
  }

  _loadCaps() {
    if (this._capsPromise) return this._capsPromise;
    const camera = this._config.camera;
    const hass = this._hass;
    if (!hass || typeof hass.callWS !== "function") return Promise.resolve();
    this._capsPromise = Promise.resolve()
      .then(() => hass.callWS({ type: "camera/capabilities", entity_id: camera }))
      .then(
        (caps) => caps || null,
        (err) => {
          debug("camera/capabilities unavailable:", (err && (err.message || err.code)) || err);
          return null;
        }
      )
      .then((caps) => {
        this._capsPromise = null;
        if (!this._config || this._config.camera !== camera) return;
        this._caps = caps;
        this._capsFor = camera;
        this._render();
      });
    return this._capsPromise;
  }

  _label() {
    if (this._config.answered_by) return String(this._config.answered_by);
    const device = this._t(guessDevice());
    const user = this._hass && this._hass.user && this._hass.user.name;
    return user ? `${user} (${device})` : device.charAt(0).toUpperCase() + device.slice(1);
  }

  _title() {
    if (this._config.name) return String(this._config.name);
    const st = this._stateObj();
    return (st && st.attributes && st.attributes.friendly_name) || this._t("default_name");
  }

  _lang() {
    const hass = this._hass;
    const lang = String((hass && ((hass.locale && hass.locale.language) || hass.language)) || "");
    return lang.toLowerCase().startsWith("fr") ? "fr" : "en";
  }

  _t(key, vars) {
    const table = STRINGS[this._lang()] || STRINGS.en;
    let text = table[key] !== undefined ? table[key] : STRINGS.en[key];
    if (text === undefined) text = key;
    if (vars) {
      text = text.replace(/\{(\w+)\}/g, (match, name) =>
        vars[name] === undefined || vars[name] === null ? "" : String(vars[name])
      );
    }
    return text;
  }

  _formatTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const hass = this._hass;
    const locale = (hass && ((hass.locale && hass.locale.language) || hass.language)) || undefined;
    try {
      return date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
    } catch (err) {
      return "";
    }
  }

  /* ---------- call state ---------- */

  _update() {
    if (!this._config || !this._hass || !this._built) return;
    this._syncCall();
    if (this._needsCaps() && !this._capsPromise) this._loadCaps();
    this._render();
  }

  /** Follow the call sensor and react to its transitions. */
  _syncCall() {
    const next = this._readCall();
    this._call = next;
    if (!this.isConnected || this._isPreview) return;
    const prev = this._callSeen;
    this._callSeen = next;

    // Our own call first, so that a ring right after it ended still rings here.
    // Until the sensor shows our answer, its older state says nothing about our call.
    if (this._ownCall && next.entity) {
      if (next.state === "in_call" && next.answeredBy === this._myLabel) {
        this._callConfirmed = true;
      } else if (this._callConfirmed) {
        if (next.state === "in_call" && next.answeredBy) this._endOwnCall("taken_over", next.answeredBy);
        else if (next.state === "idle" || next.state === "ringing") this._endOwnCall("ended");
      }
    }

    const ringing = next.state === "ringing";
    const wasRinging = prev.state === "ringing";
    if (ringing && (!wasRinging || next.ringStarted !== prev.ringStarted)) this._onRingStart();
    else if (!ringing && wasRinging) this._onRingEnd();
  }

  _onRingStart() {
    debug("ringing");
    this._ringAck = null;
    if (this._mode === "talk") return;
    setText(this._els.announcer, this._t("ringing"));
    if (this._config.ring_sound) this._startRingtone();
    if (!this._videoReady) this._refreshPoster(true); // who is there, until the video is up
    if (this._config.preview_on_ring !== false && !this._pc && this._mode === "none") this._connect("preview");
    if (this._config.fullscreen_on_ring && !this._isFullscreen()) this._enterFullscreen(true);
  }

  _onRingEnd() {
    debug("ringing over");
    this._stopRingtone();
    this._ringAck = null;
    setText(this._els.announcer, "");
    if (this._mode === "preview") this._teardown();
    else if (this._mode === "none" && this._fsAuto) this._exitFullscreen();
  }

  _endOwnCall(reason, who) {
    debug("call ended elsewhere:", reason, who || "");
    this._teardown();
    this._flash(reason === "taken_over" ? this._t("taken_over", { who }) : this._t("call_ended"));
  }

  _callService(service, data, notify, entityId) {
    const entity = entityId || this._callEntityId();
    const hass = this._hass;
    if (!entity || !hass || !hass.states[entity]) return Promise.resolve();
    debug("service", `${DOMAIN}.${service}`, data || {});
    return Promise.resolve()
      .then(() => hass.callService(DOMAIN, service, { entity_id: entity, ...(data || {}) }, undefined, notify))
      .catch((err) => debug(`${service} failed:`, (err && (err.message || err.code)) || err));
  }

  _keepAlive() {
    if (!this._ownCall || this._media === "error") return;
    const call = this._call;
    if (call.state === "in_call" && call.answeredBy && call.answeredBy !== this._myLabel) return;
    this._callService("answer", { answered_by: this._myLabel }, false);
  }

  /* ---------- user actions ---------- */

  _answer() {
    if (!this._config || !this._hass || !this._stateObj()) return;
    this._unlockMedia();
    this._stopRingtone();
    if (this._call.state === "ringing") this._ringAck = this._ringKey();
    this._retries = 0;
    const talk = this._canTalk();
    if (!this._ownCall) {
      this._ownCall = true;
      this._callConfirmed = false;
      this._callStart = Date.now();
      this._myLabel = this._label();
    }
    this._mode = "talk";
    this._speakerOn = true;
    this._soundBlocked = false;
    this._micError = null;
    this._pendingAnswer = this._callService("answer", { answered_by: this._myLabel }, true);
    this._setInterval("keepalive", () => this._keepAlive(), KEEPALIVE_MS);
    this._setInterval("clock", () => this._renderClock(), 1000);
    this._acquireWakeLock();
    // Same connection as "Voir" or the ring preview: only the microphone changes.
    const reuse =
      this._pc && this._media !== "error" && !(talk && this._audioTx && this._audioTx.direction !== "sendrecv");
    if (reuse) {
      this._applyAudio();
    } else {
      this._closeSession();
      this._connect("talk");
    }
    if (talk) {
      this._micWanted = !this._config.push_to_talk;
      this._acquireMic();
    }
    this._render();
  }

  _view() {
    if (!this._config || !this._hass || !this._stateObj() || this._mode === "talk") return;
    this._unlockMedia();
    this._stopRingtone();
    if (this._call.state === "ringing") this._ringAck = this._ringKey();
    this._retries = 0;
    this._mode = "view";
    this._soundBlocked = false;
    this._acquireWakeLock();
    if (this._pc && this._media !== "error") {
      this._applyAudio();
    } else {
      this._closeSession();
      this._connect("view");
    }
    this._render();
  }

  _dismiss() {
    this._stopRingtone();
    this._ringAck = this._ringKey();
    this._callService("dismiss", {}, true);
    if (this._mode === "preview") this._teardown();
    else if (this._mode === "none" && this._fsAuto) this._exitFullscreen();
    this._render();
  }

  _close() {
    if (this._ownCall) {
      this._hangUp();
      return;
    }
    this._teardown();
    this._render();
  }

  _hangUp() {
    const own = this._ownCall;
    const label = this._myLabel;
    const pending = this._pendingAnswer;
    const entity = this._callEntityId(); // now: the config may change before the answer settles
    this._teardown();
    this._render();
    if (!own || !entity) return;
    // After our own "answer" settled, so the integration sees them in order.
    Promise.resolve(pending).then(() => {
      const call = this._call;
      if (call.entity === entity && call.state === "in_call" && call.answeredBy && call.answeredBy !== label) {
        debug("the call now belongs to", call.answeredBy, "- not hanging up");
        return;
      }
      this._callService("hang_up", {}, true, entity);
    });
  }

  _retry() {
    const mode = this._mode === "none" || this._mode === "preview" ? "view" : this._mode;
    this._unlockMedia();
    this._retries = 0;
    this._closeSession();
    this._connect(mode);
    if (mode === "talk" && this._canTalk()) this._acquireMic();
  }

  _unmute() {
    this._speakerOn = true;
    this._soundBlocked = false;
    this._applyAudio();
    this._render();
  }

  _toggleSpeaker() {
    if (this._soundBlocked) {
      this._speakerOn = true;
      this._soundBlocked = false;
    } else {
      this._speakerOn = !this._speakerOn;
    }
    this._applyAudio();
    this._render();
  }

  _toggleFullscreen() {
    if (this._isFullscreen()) this._exitFullscreen();
    else this._enterFullscreen(false);
  }

  /* ---------- answer from a notification ---------- */

  _checkAutoAnswer() {
    if (!this._config || !this._hass || !this.isConnected || this._isPreview || !this._stateObj()) return;
    let params;
    try {
      params = new URLSearchParams(window.location.search);
    } catch (err) {
      return;
    }
    if (!params.has("ajax_answer")) return;
    const value = params.get("ajax_answer");
    if (value !== "1" && value !== this._config.camera) return;
    // Removing the parameter is also what keeps a second card from answering.
    params.delete("ajax_answer");
    const query = params.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash || ""}`;
    try {
      window.history.replaceState(window.history.state, "", url);
    } catch (err) {
      debug("replaceState failed:", err);
    }
    debug("answering from the URL (ajax_answer=%s)", value);
    this._answer();
  }

  /* ---------- WebRTC ---------- */

  async _connect(mode) {
    if (!this._hass || !this._config) return;
    if (this._pc) {
      this._mode = mode;
      this._applyAudio();
      this._render();
      return;
    }
    const gen = ++this._gen;
    const entityId = this._config.camera;
    this._mode = mode;
    this._media = "connecting";
    this._errorText = "";
    this._videoReady = false;
    this._sessionId = null;
    this._localCandidates = [];
    this._remoteCandidates = [];
    this._remoteReady = false;
    this._setTimeout("connect", () => this._fail("timeout", null, gen), CONNECT_TIMEOUT_MS);
    try {
      this._render();
      if (typeof RTCPeerConnection !== "function") throw new Error("WebRTC is not available in this browser");
      const [clientConfig] = await Promise.all([
        this._clientConfig(entityId),
        this._needsCaps() ? this._loadCaps() : null,
      ]);
      if (gen !== this._gen) return;

      const pc = new RTCPeerConnection((clientConfig && clientConfig.configuration) || {});
      this._pc = pc;
      const talk = this._canTalk();
      // Audio first, then video, like Home Assistant's own player. With talk-back
      // through go2rtc the audio transceiver is sendrecv but carries no track yet:
      // answering swaps the microphone in with replaceTrack, without renegotiating.
      // With the integration's relay, the voice goes another way: receive only.
      const sendAudio = talk && !this._relayMode();
      this._audioTx = pc.addTransceiver("audio", { direction: sendAudio ? "sendrecv" : "recvonly" });
      pc.addTransceiver("video", { direction: "recvonly" });
      if (clientConfig && clientConfig.dataChannel) pc.createDataChannel(clientConfig.dataChannel);
      this._remoteStream = new MediaStream();
      pc.ontrack = (event) => this._onTrack(gen, event);
      pc.onicecandidate = (event) => this._onLocalCandidate(gen, event);
      pc.onconnectionstatechange = () => this._onPcState(gen);
      pc.oniceconnectionstatechange = () => {
        debug("ICE:", pc.iceConnectionState);
        if (!("connectionState" in pc)) this._onPcState(gen);
      };
      this._syncMic();

      const offer = await pc.createOffer();
      if (gen !== this._gen) return;
      await pc.setLocalDescription(offer);
      if (gen !== this._gen) return;
      debug("offer:", mode, talk ? "talk-back" : "listen only", `${offer.sdp.length} bytes`);

      const unsub = await this._hass.connection.subscribeMessage(
        (msg) => this._onSignal(gen, msg),
        { type: "camera/webrtc/offer", entity_id: entityId, offer: offer.sdp },
        { resubscribe: false }
      );
      if (gen !== this._gen) {
        Promise.resolve()
          .then(() => unsub())
          .catch(() => {});
        return;
      }
      this._unsub = unsub;
    } catch (err) {
      if (gen !== this._gen) return;
      const message = (err && (err.message || err.code)) || String(err);
      const unsupported =
        (err && err.code === "webrtc_get_client_config_failed") || /does not support webrtc/i.test(message);
      this._fail(unsupported ? "webrtc" : "exception", message, gen);
    }
  }

  async _clientConfig(entityId) {
    try {
      const config = await this._hass.callWS({ type: "camera/webrtc/get_client_config", entity_id: entityId });
      return config || {};
    } catch (err) {
      if (err && err.code === "webrtc_get_client_config_failed") throw err;
      debug("get_client_config failed:", (err && (err.message || err.code)) || err);
      return {};
    }
  }

  _onSignal(gen, msg) {
    if (gen !== this._gen || !this._pc || !msg) return;
    const pc = this._pc;
    switch (msg.type) {
      case "session":
        if (this._sessionId) return;
        this._sessionId = msg.session_id;
        debug("session", msg.session_id);
        this._flushLocalCandidates();
        break;
      case "answer":
        if (pc.signalingState !== "have-local-offer") {
          debug("answer ignored in state", pc.signalingState);
          return;
        }
        debug("answer:", `${(msg.answer || "").length} bytes`);
        pc.setRemoteDescription({ type: "answer", sdp: msg.answer }).then(
          () => {
            if (gen !== this._gen) return;
            this._remoteReady = true;
            const queued = this._remoteCandidates;
            this._remoteCandidates = [];
            queued.forEach((candidate) => this._addRemoteCandidate(gen, candidate));
          },
          (err) => this._fail("signal", err && err.message, gen)
        );
        break;
      case "candidate": {
        const candidate = this._normalizeCandidate(msg.candidate);
        if (!candidate) return;
        if (this._remoteReady) this._addRemoteCandidate(gen, candidate);
        else this._remoteCandidates.push(candidate);
        break;
      }
      case "error":
        debug("signaling error:", msg.code, msg.message);
        this._fail("signal", msg.message || msg.code, gen);
        break;
      default:
        break;
    }
  }

  _normalizeCandidate(raw) {
    if (!raw) return null;
    const source = typeof raw === "string" ? { candidate: raw } : raw;
    if (!source.candidate) return null;
    const candidate = {
      candidate: source.candidate,
      sdpMid: source.sdpMid === undefined ? null : source.sdpMid,
      sdpMLineIndex: source.sdpMLineIndex === undefined ? null : source.sdpMLineIndex,
    };
    if (candidate.sdpMid === null && candidate.sdpMLineIndex === null) candidate.sdpMLineIndex = 0;
    return candidate;
  }

  _addRemoteCandidate(gen, candidate) {
    if (gen !== this._gen || !this._pc) return;
    this._pc
      .addIceCandidate(candidate)
      .catch((err) => debug("addIceCandidate failed:", (err && err.message) || err));
  }

  _onLocalCandidate(gen, event) {
    if (gen !== this._gen) return;
    const cand = event && event.candidate;
    // End of gathering (null or empty candidate): go2rtc needs no marker.
    if (!cand || !cand.candidate) return;
    const init = { candidate: cand.candidate, sdpMid: cand.sdpMid, sdpMLineIndex: cand.sdpMLineIndex };
    if (!this._sessionId) {
      this._localCandidates.push(init);
      return;
    }
    this._sendCandidate(init);
  }

  _flushLocalCandidates() {
    const queued = this._localCandidates;
    this._localCandidates = [];
    queued.forEach((candidate) => this._sendCandidate(candidate));
  }

  _sendCandidate(candidate) {
    const hass = this._hass;
    if (!hass || !this._sessionId) return;
    hass.connection
      .sendMessagePromise({
        type: "camera/webrtc/candidate",
        entity_id: this._config.camera,
        session_id: this._sessionId,
        candidate,
      })
      .catch((err) => debug("candidate rejected:", (err && (err.message || err.code)) || err));
  }

  _onTrack(gen, event) {
    if (gen !== this._gen || !this._remoteStream) return;
    debug("remote track:", event.track && event.track.kind);
    if (event.track) this._remoteStream.addTrack(event.track);
    // Audio and video tracks arrive back to back: attach the stream once both are in.
    this._setTimeout(
      "attach",
      () => {
        if (gen !== this._gen || !this._remoteStream) return;
        const video = this._els.video;
        if (video.srcObject !== this._remoteStream) video.srcObject = this._remoteStream;
        this._applyAudio();
      },
      0
    );
  }

  _onPcState(gen) {
    const pc = this._pc;
    if (gen !== this._gen || !pc) return;
    let state = pc.connectionState;
    if (!state) state = pc.iceConnectionState === "completed" ? "connected" : pc.iceConnectionState;
    debug("connection:", state);
    if (state === "connected") {
      this._clearTimer("connect");
      this._clearTimer("grace");
      const first = this._media !== "live";
      this._media = "live";
      this._reconnecting = false;
      if (first) {
        this._setTimeout("stable", () => (this._retries = 0), STABLE_MS);
        if (this._config.show_stats) this._startStats();
      }
      this._render();
    } else if (state === "disconnected") {
      if (this._media !== "live") return; // still connecting: the connect timeout covers it
      this._reconnecting = true;
      this._render();
      this._setTimeout(
        "grace",
        () => {
          if (gen === this._gen && this._pc && this._pc.connectionState !== "connected") {
            this._fail("lost", null, gen);
          }
        },
        DISCONNECTED_GRACE_MS
      );
    } else if (state === "failed") {
      this._fail(this._media === "live" ? "lost" : "ice", null, gen);
    }
  }

  _fail(code, detail, gen) {
    if (gen !== undefined && gen !== this._gen) return;
    const mode = this._mode;
    debug("session failed:", code, detail || "");
    this._closeSession();
    if (mode === "none") {
      this._render();
      return;
    }
    if (code !== "webrtc" && this._config.auto_retry !== false && this._retries < 1) {
      this._retries += 1;
      this._media = "connecting";
      this._reconnecting = true;
      this._setTimeout(
        "retry",
        () => {
          if (this._mode === mode && !this._pc) this._connect(mode);
        },
        RETRY_DELAY_MS
      );
      this._render();
      return;
    }
    if (mode === "preview") {
      // Quietly drop the preview: the ring overlay stays and answering reconnects.
      this._mode = "none";
      this._render();
      return;
    }
    this._media = "error";
    this._errorText = this._errorMessage(code, detail);
    if (this._micTrack) this._releaseMic(); // nothing to send: give the microphone back
    this._render();
  }

  _errorMessage(code, detail) {
    switch (code) {
      case "ice":
        return this._t("err_ice");
      case "lost":
        return this._t("err_lost");
      case "timeout":
        return this._t("err_timeout");
      case "webrtc":
        return this._t("err_webrtc");
      case "signal":
        return detail ? this._t("err_signal", { message: detail }) : this._t("err_lost");
      default:
        return this._t("err_generic", { message: detail || "?" });
    }
  }

  /** Close the peer connection and the HA session; keeps the mode, the call and the mic. */
  _closeSession() {
    this._gen += 1;
    ["connect", "grace", "stats", "attach", "stable"].forEach((name) => this._clearTimer(name));
    const unsub = this._unsub;
    this._unsub = null;
    if (unsub) {
      Promise.resolve()
        .then(() => unsub())
        .catch(() => {});
    }
    const pc = this._pc;
    this._pc = null;
    this._audioTx = null;
    this._sentTrack = null;
    if (pc) {
      pc.ontrack = null;
      pc.onicecandidate = null;
      pc.onconnectionstatechange = null;
      pc.oniceconnectionstatechange = null;
      try {
        pc.close();
      } catch (err) {
        /* already closed */
      }
    }
    if (this._remoteStream) {
      this._remoteStream.getTracks().forEach((track) => {
        try {
          track.stop();
        } catch (err) {
          /* ignore */
        }
      });
      this._remoteStream = null;
    }
    const video = this._els.video;
    if (video && video.srcObject) {
      try {
        video.pause();
      } catch (err) {
        /* ignore */
      }
      video.srcObject = null;
    }
    const hadVideo = this._videoReady;
    this._videoReady = false;
    this._reconnecting = false;
    this._sessionId = null;
    this._localCandidates = [];
    this._remoteCandidates = [];
    this._remoteReady = false;
    this._statsPrev = null;
    this._micLive = false;
    this._media = "idle";
    if (hadVideo) this._refreshPoster(true);
  }

  /** Back to idle: no connection, no microphone, no call. */
  _teardown() {
    ["retry", "keepalive", "clock"].forEach((name) => this._clearTimer(name));
    this._closeSession();
    this._releaseMic();
    this._micWanted = false;
    this._micError = null;
    this._pttDown = false;
    this._mode = "none";
    this._ownCall = false;
    this._callConfirmed = false;
    this._myLabel = null;
    this._pendingAnswer = null;
    this._soundBlocked = false;
    this._errorText = "";
    this._retries = 0;
    this._releaseWakeLock();
    if (this._isFullscreen()) this._exitFullscreen();
  }

  /* ---------- playback ---------- */

  _applyAudio() {
    const video = this._els.video;
    if (!video) return;
    const wantSound = this._mode !== "preview" && this._mode !== "none" && this._speakerOn;
    video.muted = !wantSound;
    if (!video.srcObject) return;
    let playing;
    try {
      playing = video.play();
    } catch (err) {
      playing = null;
    }
    if (!playing || typeof playing.then !== "function") return;
    playing.then(
      () => {
        if (wantSound && this._soundBlocked && !video.muted) {
          this._soundBlocked = false;
          this._render();
        }
      },
      (err) => {
        const name = err && err.name;
        if (name !== "NotAllowedError" || !wantSound) {
          debug("play():", name || err);
          return;
        }
        // Autoplay with sound refused (no user gesture yet): play muted, offer a button.
        debug("autoplay with sound refused: playing muted");
        video.muted = true;
        this._soundBlocked = true;
        const retry = video.play();
        if (retry && typeof retry.catch === "function") retry.catch(() => {});
        this._render();
      }
    );
  }

  /** Called inside a user gesture: lets Safari play this element with sound later. */
  _unlockMedia() {
    const video = this._els.video;
    if (!video || video.srcObject) return;
    try {
      video.load();
    } catch (err) {
      /* ignore */
    }
  }

  /* ---------- microphone ---------- */

  async _acquireMic() {
    if (this._mode !== "talk") return;
    if (this._micTrack) {
      this._syncMic();
      this._render();
      return;
    }
    if (this._micBusy) return;
    if (typeof window !== "undefined" && window.isSecureContext === false) {
      this._micError = "insecure";
      this._render();
      return;
    }
    const devices = typeof navigator !== "undefined" ? navigator.mediaDevices : null;
    if (!devices || typeof devices.getUserMedia !== "function") {
      this._micError = "unsupported";
      this._render();
      return;
    }
    const req = ++this._micReq;
    this._micBusy = true;
    this._micError = null;
    this._render();
    let stream;
    try {
      stream = await devices.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      if (req !== this._micReq) return;
      this._micBusy = false;
      this._micError = (err && err.name) || "Error";
      debug("getUserMedia failed:", this._micError, (err && err.message) || "");
      this._render();
      return;
    }
    const track = stream.getAudioTracks()[0];
    if (req !== this._micReq || this._mode !== "talk" || !track) {
      stream.getTracks().forEach((t) => t.stop());
      if (req === this._micReq) this._micBusy = false;
      return;
    }
    this._micBusy = false;
    this._micTrack = track;
    if (this._relayMode()) this._startRelay(track);
    track.onended = () => {
      // Unplugged, or the permission was revoked.
      if (this._micTrack !== track) return;
      debug("microphone track ended");
      this._micTrack = null;
      this._syncMic();
      this._render();
    };
    this._syncMic();
    this._render();
  }

  /** Put the microphone on the audio sender, or take it off, to match what the user wants. */
  _syncMic() {
    const wanted = this._config && this._config.push_to_talk ? this._pttDown : this._micWanted;
    const live = this._mode === "talk" && Boolean(this._micTrack) && wanted;
    if (this._relay) {
      this._relay.setLive(live);
      this._micLive = live && this._relay.ready;
    } else {
      this._setSenderTrack(live ? this._micTrack : null);
      this._micLive = live && Boolean(this._audioTx) && !this._relayMode();
    }
    this._updateMeter(live);
  }

  /**
   * replaceTrack() resolves later and only then updates sender.track, so compare
   * with what we last asked for: a quick push-to-talk tap must not leave the mic on.
   */
  _setSenderTrack(track) {
    const sender = this._audioTx && this._audioTx.sender;
    if (!sender || this._sentTrack === track) return;
    this._sentTrack = track;
    sender.replaceTrack(track).catch((err) => debug("replaceTrack failed:", (err && err.message) || err));
  }

  /** Open the integration's relay for this microphone; the mic is live once it is ready. */
  async _startRelay(track) {
    if (this._relay) return;
    const label = this._myLabel || this._label();
    const relay = new TalkRelay(this._hass, this._config.camera, label, (reason) => {
      if (this._relay !== relay) return;
      this._relay = null;
      this._micWanted = false;
      this._releaseMic();
      this._micError = reason === "taken_over" ? "taken_over" : "relay_ended";
      this._render();
    });
    this._relay = relay;
    try {
      await relay.start(track);
    } catch (err) {
      if (this._relay !== relay) return;
      this._relay = null;
      relay.stop();
      const message = (err && (err.message || err.code)) || String(err);
      debug("talk relay failed:", message);
      this._releaseMic();
      this._micError = `relay:${message}`;
      this._render();
      return;
    }
    if (this._relay !== relay) return;
    debug("talk relay ready:", relay.codec);
    this._syncMic();
    this._render();
  }

  /** Detach and stop the microphone track so the browser releases the mic. */
  _releaseMic() {
    this._micReq += 1;
    this._micBusy = false;
    const track = this._micTrack;
    this._micTrack = null;
    if (this._relay) {
      const relay = this._relay;
      this._relay = null;
      relay.stop();
    }
    this._setSenderTrack(null);
    if (track) {
      track.onended = null;
      try {
        track.stop();
      } catch (err) {
        /* ignore */
      }
    }
    this._micLive = false;
    this._updateMeter(false);
  }

  _toggleMic() {
    if (this._mode !== "talk") return;
    const on = this._micWanted && Boolean(this._micTrack || this._micBusy);
    if (on) {
      this._micWanted = false;
      this._releaseMic();
    } else {
      this._micWanted = true;
      this._micError = null;
      this._acquireMic();
    }
    this._render();
  }

  _pttStart() {
    if (this._pttDown || this._mode !== "talk") return;
    this._pttDown = true;
    if (this._micTrack) this._syncMic();
    else this._acquireMic();
    this._refreshControls();
  }

  _pttEnd() {
    if (!this._pttDown) return;
    this._pttDown = false;
    this._syncMic();
    this._refreshControls();
  }

  /** Light the mic button with the voice level, so you can see the doorbell hears you. */
  _updateMeter(on) {
    if (!on || !this._micTrack) {
      if (this._meter) {
        if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(this._meter.raf);
        try {
          this._meter.source.disconnect();
        } catch (err) {
          /* ignore */
        }
        this._meter = null;
      }
      this._setLevel(0);
      return;
    }
    if (this._meter && this._meter.track === this._micTrack) return;
    if (this._meter) this._updateMeter(false);
    if (typeof requestAnimationFrame !== "function") return;
    const ctx = this._getAudioCtx();
    if (!ctx) return;
    try {
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const source = ctx.createMediaStreamSource(new MediaStream([this._micTrack]));
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const meter = { track: this._micTrack, source, raf: 0, last: 0 };
      const tick = (time) => {
        if (this._meter !== meter) return;
        meter.raf = requestAnimationFrame(tick);
        if (time - meter.last < 66) return;
        meter.last = time;
        analyser.getByteTimeDomainData(samples);
        let sum = 0;
        for (let i = 0; i < samples.length; i += 1) {
          const v = (samples[i] - 128) / 128;
          sum += v * v;
        }
        this._setLevel(Math.min(1, Math.sqrt(sum / samples.length) * 4));
      };
      this._meter = meter;
      meter.raf = requestAnimationFrame(tick);
    } catch (err) {
      debug("level meter unavailable:", err);
    }
  }

  _setLevel(level) {
    const button = this._ctrl.mic || this._ctrl.ptt;
    if (button) button.style.setProperty("--lvl", level.toFixed(2));
  }

  _getAudioCtx() {
    if (this._audioCtx && this._audioCtx.state !== "closed") return this._audioCtx;
    const Ctx = typeof window !== "undefined" && (window.AudioContext || window.webkitAudioContext);
    if (!Ctx) return null;
    try {
      this._audioCtx = new Ctx();
    } catch (err) {
      this._audioCtx = null;
    }
    return this._audioCtx;
  }

  /* ---------- ringtone (Web Audio, no file) ---------- */

  _startRingtone() {
    if (this._ringtone) return;
    const ctx = this._getAudioCtx();
    if (!ctx) return;
    const master = ctx.createGain();
    master.gain.value = 0.55;
    master.connect(ctx.destination);
    const ringtone = { ctx, master, timer: null, unlock: null, last: -Infinity };
    this._ringtone = ringtone;
    const chime = () => {
      if (this._ringtone !== ringtone || ctx.state !== "running") return;
      if (ctx.currentTime - ringtone.last < 1) return;
      ringtone.last = ctx.currentTime;
      const t0 = ctx.currentTime + 0.03;
      this._bell(ctx, master, 659.25, t0, 1.2); // E5: "ding"
      this._bell(ctx, master, 523.25, t0 + 0.55, 1.7); // C5: "dong"
    };
    ringtone.timer = setInterval(chime, RING_REPEAT_MS);
    if (ctx.state === "running") {
      chime();
      return;
    }
    // Browsers keep audio suspended until the user has interacted with the page.
    Promise.resolve()
      .then(() => ctx.resume())
      .then(chime, () => {});
    ringtone.unlock = () => {
      Promise.resolve()
        .then(() => ctx.resume())
        .then(chime, () => {});
    };
    window.addEventListener("pointerdown", ringtone.unlock, true);
    window.addEventListener("keydown", ringtone.unlock, true);
  }

  _bell(ctx, destination, frequency, start, duration) {
    // A few inharmonic partials with fast decays sound like a chime, not a beep.
    [
      [1, 0.6],
      [2.76, 0.14],
      [5.4, 0.05],
    ].forEach(([ratio, gain]) => {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      const end = start + duration / Math.sqrt(ratio);
      osc.type = "sine";
      osc.frequency.value = frequency * ratio;
      env.gain.setValueAtTime(0.0001, start);
      env.gain.exponentialRampToValueAtTime(gain, start + 0.012);
      env.gain.exponentialRampToValueAtTime(0.0001, end);
      osc.connect(env);
      env.connect(destination);
      osc.start(start);
      osc.stop(end + 0.05);
      osc.onended = () => {
        try {
          env.disconnect();
        } catch (err) {
          /* ignore */
        }
      };
    });
  }

  _stopRingtone() {
    const ringtone = this._ringtone;
    if (!ringtone) return;
    this._ringtone = null;
    clearInterval(ringtone.timer);
    if (ringtone.unlock) {
      window.removeEventListener("pointerdown", ringtone.unlock, true);
      window.removeEventListener("keydown", ringtone.unlock, true);
    }
    try {
      ringtone.master.gain.setTargetAtTime(0, ringtone.ctx.currentTime, 0.05);
    } catch (err) {
      /* ignore */
    }
    setTimeout(() => {
      try {
        ringtone.master.disconnect();
      } catch (err) {
        /* ignore */
      }
    }, 400);
  }

  /* ---------- full screen ---------- */

  _isFullscreen() {
    return this._fsReal || this._fsPseudo;
  }

  async _enterFullscreen(auto) {
    this._fsAuto = Boolean(auto);
    const el = this._els.card;
    const request = el && (el.requestFullscreen || el.webkitRequestFullscreen);
    if (request) {
      // Old WebKit returns nothing and some web views never settle: fall back if nothing happens.
      this._setTimeout(
        "fs-check",
        () => {
          if (!this._fsReal && !this._fsPseudo) {
            this._fsPseudo = true;
            this._render();
          }
        },
        FS_FALLBACK_MS
      );
      try {
        await request.call(el);
        return;
      } catch (err) {
        // Refused (no user gesture when it rings, iPhone...): fill the page instead.
        this._clearTimer("fs-check");
        debug("full screen refused, using the page:", (err && err.name) || err);
      }
    }
    this._fsPseudo = true;
    this._render();
  }

  _exitFullscreen() {
    this._fsAuto = false;
    this._clearTimer("fs-check");
    if (this._fsPseudo) {
      this._fsPseudo = false;
      this._render();
    }
    if (this._fsReal) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) {
        try {
          const result = exit.call(document);
          if (result && typeof result.catch === "function") result.catch(() => {});
        } catch (err) {
          /* ignore */
        }
      }
    }
  }

  _handleFsChange() {
    const root = this.shadowRoot;
    const element =
      (root && root.fullscreenElement) || document.fullscreenElement || document.webkitFullscreenElement;
    const real = Boolean(element) && (element === this._els.card || element === this);
    if (real === this._fsReal) return;
    this._fsReal = real;
    if (real) {
      this._fsPseudo = false;
      this._clearTimer("fs-check");
    } else {
      this._fsAuto = false;
    }
    this._render();
  }

  /* ---------- wake lock, visibility ---------- */

  _acquireWakeLock() {
    if (this._wakeLock || this._wakeLockPending) return;
    const wakeLock = typeof navigator !== "undefined" && navigator.wakeLock;
    if (!wakeLock || typeof wakeLock.request !== "function" || document.visibilityState !== "visible") return;
    this._wakeLockPending = true;
    Promise.resolve()
      .then(() => wakeLock.request("screen"))
      .then(
        (lock) => {
          this._wakeLockPending = false;
          if (this._mode !== "view" && this._mode !== "talk") {
            lock.release().catch(() => {});
            return;
          }
          this._wakeLock = lock;
          lock.addEventListener("release", () => {
            if (this._wakeLock === lock) this._wakeLock = null;
          });
        },
        (err) => {
          this._wakeLockPending = false;
          debug("wake lock refused:", (err && err.name) || err);
        }
      );
  }

  _releaseWakeLock() {
    const lock = this._wakeLock;
    this._wakeLock = null;
    if (lock) {
      Promise.resolve()
        .then(() => lock.release())
        .catch(() => {});
    }
  }

  _handleVisibility() {
    if (document.visibilityState === "visible") {
      const video = this._els.video;
      if (this._pc && video && video.srcObject && video.paused) this._applyAudio();
      if (this._mode === "view" || this._mode === "talk") this._acquireWakeLock();
      this._refreshPoster(false);
    } else {
      this._pttEnd();
    }
  }

  /* ---------- poster ---------- */

  _refreshPoster(force) {
    if (!this._built || !this._hass || !this._config) return;
    const hidden = typeof document !== "undefined" && document.hidden;
    if (!force && (!this._visible || hidden || this._videoReady)) return;
    const st = this._stateObj();
    const picture = st && st.attributes && st.attributes.entity_picture;
    if (!picture) {
      if (this._posterLoaded) {
        this._posterLoaded = false;
        this._render();
      }
      return;
    }
    const base = typeof this._hass.hassUrl === "function" ? this._hass.hassUrl(picture) : picture;
    const url = `${base}${base.includes("?") ? "&" : "?"}_ts=${Date.now()}`;
    const req = ++this._posterReq;
    // Load off-screen first, so the old picture stays until the new one is ready.
    const img = new Image();
    img.onload = () => {
      if (req !== this._posterReq || !this._els.poster) return;
      this._els.poster.src = url;
      this._setAspect(img.naturalWidth, img.naturalHeight, true);
      if (!this._posterLoaded) {
        this._posterLoaded = true;
        this._render();
      }
    };
    img.onerror = () => {
      if (req === this._posterReq) debug("snapshot unavailable");
    };
    img.src = url;
  }

  _applyAspect() {
    const forced = normalizeAspect(this._config && this._config.aspect_ratio);
    this._aspectForced = Boolean(forced);
    this._aspectFromVideo = false;
    if (this._els.stage) this._els.stage.style.setProperty("--adb-aspect", forced || "16 / 9");
  }

  _setAspect(width, height, fromPoster) {
    if (this._aspectForced || !width || !height || !this._els.stage) return;
    if (fromPoster && this._aspectFromVideo) return;
    if (!fromPoster) this._aspectFromVideo = true;
    this._els.stage.style.setProperty("--adb-aspect", `${width} / ${height}`);
  }

  /* ---------- stats ---------- */

  _startStats() {
    this._statsPrev = null;
    this._setInterval("stats", () => this._pollStats(), STATS_MS);
    this._pollStats();
  }

  async _pollStats() {
    const pc = this._pc;
    const gen = this._gen;
    if (!pc || !this._config.show_stats || typeof pc.getStats !== "function") return;
    let report;
    try {
      report = await pc.getStats();
    } catch (err) {
      return;
    }
    if (gen !== this._gen || !this._els.stats) return;
    const byId = new Map();
    report.forEach((r) => byId.set(r.id, r));
    let transport = null;
    let videoIn = null;
    let audioIn = null;
    let audioOut = null;
    let pair = null;
    byId.forEach((r) => {
      const kind = r.kind || r.mediaType;
      if (r.type === "transport") transport = r;
      else if (r.type === "inbound-rtp" && kind === "video") videoIn = r;
      else if (r.type === "inbound-rtp" && kind === "audio") audioIn = r;
      else if (r.type === "outbound-rtp" && kind === "audio") audioOut = r;
      else if (r.type === "candidate-pair" && !pair && (r.selected || (r.nominated && r.state === "succeeded"))) {
        pair = r;
      }
    });
    if (transport && byId.has(transport.selectedCandidatePairId)) pair = byId.get(transport.selectedCandidatePairId);

    const ms = (seconds) => (typeof seconds === "number" ? `${Math.round(seconds * 1000)} ms` : "—");
    const codec = (r) => {
      const c = r && r.codecId ? byId.get(r.codecId) : null;
      if (!c || !c.mimeType) return "—";
      const name = c.mimeType.split("/").pop();
      return /^audio/i.test(c.mimeType) && c.clockRate ? `${name} ${c.clockRate / 1000} kHz` : name;
    };
    const local = pair ? byId.get(pair.localCandidateId) : null;
    const path = local ? ` · ${local.candidateType || "?"}/${local.protocol || "?"}` : "";

    const lost = ((videoIn && videoIn.packetsLost) || 0) + ((audioIn && audioIn.packetsLost) || 0);
    const received = ((videoIn && videoIn.packetsReceived) || 0) + ((audioIn && audioIn.packetsReceived) || 0);
    const bytes = (videoIn && videoIn.bytesReceived) || 0;
    const now = Date.now();
    const prev = this._statsPrev;
    let loss = "—";
    let rate = "";
    if (prev) {
      const dLost = Math.max(0, lost - prev.lost);
      const dReceived = Math.max(0, received - prev.received);
      loss = `${(dLost + dReceived > 0 ? (dLost / (dLost + dReceived)) * 100 : 0).toFixed(1)} %`;
      const seconds = (now - prev.time) / 1000;
      if (seconds > 0 && bytes >= prev.bytes) rate = ` · ${Math.round(((bytes - prev.bytes) * 8) / seconds / 1000)} kb/s`;
    }
    this._statsPrev = { time: now, lost, received, bytes };

    const size = videoIn && videoIn.frameWidth ? `${videoIn.frameWidth}×${videoIn.frameHeight}` : "—";
    const fps =
      videoIn && typeof videoIn.framesPerSecond === "number" ? ` @ ${Math.round(videoIn.framesPerSecond)} fps` : "";
    const lines = [
      `${this._t("st_rtt")} ${ms(pair && pair.currentRoundTripTime)}${path}`,
      `${this._t("st_jitter")} ${ms(audioIn && audioIn.jitter)} (A) · ${ms(videoIn && videoIn.jitter)} (V)`,
      `${this._t("st_loss")} ${loss}`,
      `${this._t("st_video")} ${size}${fps} · ${codec(videoIn)}${rate}`,
      `${this._t("st_audio_in")} ${codec(audioIn)}`,
      `${this._t("st_audio_out")} ${audioOut ? codec(audioOut) : "—"}${this._micLive ? "" : ` (${this._t("st_off")})`}`,
    ];
    setText(this._els.stats, lines.join("\n"));
  }

  /* ---------- rendering ---------- */

  _build() {
    const root = this.shadowRoot;
    root.innerHTML = `<style>${STYLE}</style>${TEMPLATE}`;
    const $ = (id) => root.getElementById(id);
    this._els = {
      card: $("card"),
      inner: $("inner"),
      stage: $("stage"),
      poster: $("poster"),
      video: $("video"),
      name: $("name"),
      badge: $("badge"),
      badgeText: $("badge-text"),
      timer: $("timer"),
      timerText: $("timer-text"),
      banner: $("banner"),
      bannerIcon: $("banner-icon"),
      bannerText: $("banner-text"),
      center: $("center"),
      unmute: $("unmute"),
      unmuteText: $("unmute-text"),
      toast: $("toast"),
      stats: $("stats"),
      controls: $("controls"),
      hint: $("hint"),
      hintText: $("hint-text"),
      ring: $("ring"),
      ringTitle: $("ring-title"),
      ringSub: $("ring-sub"),
      ringAnswer: $("ring-answer"),
      ringAnswerIcon: $("ring-answer-icon"),
      ringAnswerLabel: $("ring-answer-label"),
      ringView: $("ring-view"),
      ringViewLabel: $("ring-view-label"),
      ringIgnore: $("ring-ignore"),
      ringIgnoreLabel: $("ring-ignore-label"),
      announcer: $("announcer"),
    };
    const video = this._els.video;
    video.muted = true;
    video.playsInline = true;
    const onFrames = () => {
      if (this._pc && !this._videoReady && video.videoWidth > 0) {
        this._videoReady = true;
        this._render();
      }
    };
    video.addEventListener("loadedmetadata", () => this._setAspect(video.videoWidth, video.videoHeight, false));
    video.addEventListener("resize", () => {
      this._setAspect(video.videoWidth, video.videoHeight, false);
      onFrames();
    });
    video.addEventListener("loadeddata", onFrames);
    video.addEventListener("playing", onFrames);
    this._els.unmute.addEventListener("click", () => this._unmute());
    this._els.ringAnswer.addEventListener("click", () => this._answer());
    this._els.ringView.addEventListener("click", () => this._view());
    this._els.ringIgnore.addEventListener("click", () => this._dismiss());
    this._built = true;
    this._applyAspect();
    this._refreshPoster(true);
  }

  _render() {
    if (!this._built || !this._config) return;
    const els = this._els;
    const st = this._stateObj();
    const call = this._call;
    const talk = this._canTalk();
    const title = this._title();
    const ringing = call.state === "ringing";
    const showRing = Boolean(st) && ringing && this._mode !== "talk" && this._ringAck !== this._ringKey();
    const live = this._media === "live" && Boolean(this._pc);

    els.card.classList.toggle("fs", this._isFullscreen());
    els.card.classList.toggle("pseudo-fs", this._fsPseudo);

    setText(els.name, title);
    setAttr(els.video, "aria-label", title);
    els.stage.classList.toggle("has-poster", this._posterLoaded);
    els.stage.classList.toggle("has-video", this._videoReady && Boolean(this._pc));

    let badge = "";
    if (live && this._mode !== "preview") badge = this._reconnecting ? "reconnecting" : "live";
    else if (st && st.state === "unavailable") badge = "unavailable";
    setClass(els.badge, badge ? `pill badge show ${badge}` : "pill badge");
    setText(els.badgeText, badge ? this._t(badge) : "");
    this._renderClock();

    let banner = null;
    if (st && call.entity && call.state === "in_call" && !this._ownCall) {
      const who = call.answeredBy;
      banner = {
        kind: "taken",
        icon: ICONS.inCall,
        text: who && who !== this._label() ? this._t("answered_on", { who }) : this._t("call_in_progress"),
      };
    } else if (st && ringing && !showRing && this._mode === "view") {
      banner = { kind: "ring", icon: ICONS.bell, text: this._t("ringing") };
    }
    setClass(els.banner, banner ? `banner show ${banner.kind}` : "banner");
    if (banner) {
      setAttr(els.bannerIcon, "icon", banner.icon);
      setText(els.bannerText, banner.text);
    }

    this._renderCenter(st);
    setClass(els.unmute, this._soundBlocked && this._pc && this._mode !== "preview" ? "unmute show" : "unmute");
    setText(els.unmuteText, this._t("unmute"));
    setClass(els.stats, this._config.show_stats && live ? "stats show" : "stats");

    setClass(els.ring, showRing ? "ring show" : "ring");
    els.inner.classList.toggle("ringing", showRing);
    this._inert(els.stage, showRing);
    this._inert(els.controls, showRing);
    this._inert(els.hint, showRing);
    if (showRing) {
      const time = this._formatTime(call.ringStarted);
      setText(els.ringTitle, this._t("ringing"));
      setText(els.ringSub, time ? `${title} · ${time}` : title);
      setAttr(els.ringAnswerIcon, "icon", talk ? ICONS.answer : ICONS.listen);
      setText(els.ringAnswerLabel, this._t(talk ? "answer" : "listen"));
      setText(els.ringViewLabel, this._t("view"));
      setText(els.ringIgnoreLabel, this._t("ignore"));
    }

    const screen = !st ? "missing" : this._mode === "talk" ? "call" : this._mode === "view" ? "view" : "idle";
    const key = [
      screen,
      this._media === "error" ? "error" : "ok",
      talk ? "talk" : "listen",
      this._config.push_to_talk ? "ptt" : "toggle",
      screen === "view" && ringing ? "ringing" : "",
      this._lang(),
    ].join("|");
    if (key !== this._controlsKey) {
      this._controlsKey = key;
      this._buildControls(screen, talk, ringing);
    }
    this._refreshControls();

    const hint = this._mode === "talk" && talk && this._micError ? this._micErrorText() : "";
    setClass(els.hint, hint ? "hint show" : "hint");
    setText(els.hintText, hint);
  }

  _renderClock() {
    const els = this._els;
    if (!els.timer) return;
    const show = this._mode === "talk";
    setClass(els.timer, show ? "pill timer show" : "pill timer");
    if (!show) return;
    setText(els.timerText, formatDuration((Date.now() - this._callStart) / 1000));
    setAttr(els.timer, "aria-label", this._t("call_time"));
  }

  _renderCenter(st) {
    const center = this._els.center;
    let kind = "";
    if (!st) kind = "missing";
    else if (this._media === "error") kind = "error";
    else if (this._media === "connecting" && this._mode !== "preview") kind = "connecting";
    let text = "";
    if (kind === "missing") text = this._t("not_found", { entity: this._config.camera });
    else if (kind === "error") text = this._errorText;
    else if (kind === "connecting") text = this._t(this._reconnecting ? "reconnecting" : "connecting");
    const key = `${kind}|${text}|${this._lang()}`;
    if (key === this._centerKey) return;
    this._centerKey = key;
    center.textContent = "";
    setClass(center, kind ? `center show ${kind}` : "center");
    if (!kind) return;
    if (kind === "connecting") {
      const spinner = document.createElement("div");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      center.appendChild(spinner);
    } else {
      const icon = document.createElement("ha-icon");
      icon.className = "big";
      icon.setAttribute("icon", ICONS.alert);
      center.appendChild(icon);
    }
    const msg = document.createElement("div");
    msg.className = "msg";
    msg.textContent = text;
    center.appendChild(msg);
    if (kind === "error") {
      center.appendChild(
        this._btn({ icon: ICONS.retry, label: this._t("retry"), cls: "primary", onClick: () => this._retry() })
      );
    }
  }

  _buildControls(screen, talk, ringing) {
    const container = this._els.controls;
    if (this._pttDown) this._pttEnd(); // the pressed button is about to go away
    container.textContent = "";
    this._ctrl = {};
    const add = (role, options) => {
      const button = this._btn(options);
      button.dataset.role = role;
      this._ctrl[role] = button;
      container.appendChild(button);
      return button;
    };
    const answerButton = () =>
      add("answer", {
        icon: talk ? ICONS.answer : ICONS.listen,
        label: this._t(talk ? "answer" : "listen"),
        cls: "answer wide",
        onClick: () => this._answer(),
      });
    const speakerButton = () =>
      add("speaker", { icon: ICONS.volume, label: this._t("speaker"), iconOnly: true, onClick: () => this._toggleSpeaker() });
    const fullscreenButton = () =>
      add("fs", {
        icon: ICONS.fullscreen,
        label: this._t("fullscreen"),
        iconOnly: true,
        onClick: () => this._toggleFullscreen(),
      });
    const errored = this._media === "error";

    if (screen === "missing") return;
    if (screen === "idle") {
      add("view", { icon: ICONS.view, label: this._t("view"), cls: "wide", onClick: () => this._view() });
      answerButton();
      return;
    }
    if (screen === "view") {
      if (!errored) {
        answerButton();
        speakerButton();
        fullscreenButton();
        if (ringing) {
          add("ignore", { icon: ICONS.ignore, label: this._t("ignore"), cls: "danger-soft", onClick: () => this._dismiss() });
        }
      }
      add("close", { icon: ICONS.close, label: this._t("close"), onClick: () => this._close() });
      return;
    }
    // In a call answered on this card.
    if (!errored) {
      if (talk && this._config.push_to_talk) this._addPushToTalk(container);
      else if (talk) add("mic", { icon: ICONS.mic, label: this._t("mic"), iconOnly: true, onClick: () => this._toggleMic() });
      speakerButton();
      fullscreenButton();
    }
    add("hangup", { icon: ICONS.hangup, label: this._t("hang_up"), cls: "danger wide", onClick: () => this._hangUp() });
  }

  _addPushToTalk(container) {
    const button = this._btn({ icon: ICONS.mic, label: this._t("ptt"), cls: "ptt wide" });
    button.dataset.role = "ptt";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("title", this._t("ptt_hint"));
    this._ctrl.ptt = button;
    const isSpace = (event) => event.key === " " || event.key === "Spacebar" || event.code === "Space";
    const release = () => this._pttEnd();
    button.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      try {
        button.setPointerCapture(event.pointerId);
      } catch (err) {
        /* ignore */
      }
      this._pttStart();
    });
    button.addEventListener("pointerup", release);
    button.addEventListener("pointercancel", release);
    button.addEventListener("lostpointercapture", release);
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    button.addEventListener("keydown", (event) => {
      if (!isSpace(event)) return;
      event.preventDefault();
      if (!event.repeat) this._pttStart();
    });
    button.addEventListener("keyup", (event) => {
      if (!isSpace(event)) return;
      event.preventDefault();
      this._pttEnd();
    });
    button.addEventListener("blur", release);
    container.appendChild(button);
    return button;
  }

  _btn({ icon, label, cls = "", onClick, iconOnly = false }) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn ${cls}${iconOnly ? " round" : ""}`.trim();
    const iconEl = document.createElement("ha-icon");
    iconEl.setAttribute("icon", icon);
    button.appendChild(iconEl);
    button._icon = iconEl;
    if (iconOnly) {
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
    } else {
      const span = document.createElement("span");
      span.className = "lbl";
      span.textContent = label;
      button.appendChild(span);
      button._label = span;
    }
    if (onClick) button.addEventListener("click", onClick);
    return button;
  }

  _refreshControls() {
    const c = this._ctrl;
    if (c.mic) {
      const on = this._micWanted && Boolean(this._micTrack || this._micBusy);
      setAttr(c.mic._icon, "icon", on ? ICONS.mic : ICONS.micOff);
      setAttr(c.mic, "aria-pressed", String(on));
      setAttr(c.mic, "title", this._t(on ? "mic_mute" : "mic_unmute"));
      c.mic.classList.toggle("on", on);
      c.mic.classList.toggle("busy", this._micBusy);
      c.mic.classList.toggle("warn", Boolean(this._micError) && !this._micTrack);
    }
    if (c.speaker) {
      const on = this._speakerOn && !this._soundBlocked;
      setAttr(c.speaker._icon, "icon", on ? ICONS.volume : ICONS.volumeOff);
      setAttr(c.speaker, "aria-pressed", String(on));
      setAttr(c.speaker, "title", this._t(on ? "speaker_mute" : "speaker_unmute"));
      c.speaker.classList.toggle("off", !on);
    }
    if (c.fs) {
      const fs = this._isFullscreen();
      const label = this._t(fs ? "exit_fullscreen" : "fullscreen");
      setAttr(c.fs._icon, "icon", fs ? ICONS.fullscreenExit : ICONS.fullscreen);
      setAttr(c.fs, "aria-pressed", String(fs));
      setAttr(c.fs, "aria-label", label);
      setAttr(c.fs, "title", label);
    }
    if (c.ptt) {
      const active = this._pttDown;
      c.ptt.classList.toggle("active", active);
      c.ptt.classList.toggle("busy", this._micBusy);
      c.ptt.classList.toggle("warn", Boolean(this._micError) && !this._micTrack);
      setAttr(c.ptt, "aria-pressed", String(active));
      setText(c.ptt._label, this._t(active ? "ptt_active" : "ptt"));
    }
  }

  _micErrorText() {
    const name = this._micError;
    if (name === "insecure") return this._t("mic_insecure");
    if (name === "taken_over") return this._t("mic_taken");
    if (name === "relay_ended") return this._t("mic_ended");
    if (typeof name === "string" && name.startsWith("relay:")) {
      return this._t("mic_relay", { message: name.slice(6) });
    }
    if (name === "unsupported") return this._t("mic_unsupported");
    if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
      return this._t("mic_denied");
    }
    if (name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError") {
      return this._t("mic_missing");
    }
    if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError") {
      return this._t("mic_busy");
    }
    return this._t("mic_failed", { name });
  }

  _inert(el, on) {
    if (!el) return;
    if (on && !el.hasAttribute("inert")) el.setAttribute("inert", "");
    else if (!on && el.hasAttribute("inert")) el.removeAttribute("inert");
  }

  _flash(text) {
    const toast = this._els.toast;
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add("show");
    this._setTimeout("toast", () => toast.classList.remove("show"), FLASH_MS);
  }

  /* ---------- timers ---------- */

  _setTimeout(name, fn, ms) {
    this._clearTimer(name);
    const id = setTimeout(() => {
      this._timers.delete(name);
      fn();
    }, ms);
    this._timers.set(name, { id, repeat: false });
  }

  _setInterval(name, fn, ms) {
    this._clearTimer(name);
    this._timers.set(name, { id: setInterval(fn, ms), repeat: true });
  }

  _clearTimer(name) {
    const timer = this._timers.get(name);
    if (!timer) return;
    if (timer.repeat) clearInterval(timer.id);
    else clearTimeout(timer.id);
    this._timers.delete(name);
  }

  _clearAllTimers() {
    Array.from(this._timers.keys()).forEach((name) => this._clearTimer(name));
  }
}

if (!customElements.get("ajax-doorbell-card")) {
  customElements.define("ajax-doorbell-card", AjaxDoorbellCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "ajax-doorbell-card")) {
  const french = /^fr/i.test(
    (typeof document !== "undefined" && document.documentElement && document.documentElement.lang) ||
      (typeof navigator !== "undefined" && navigator.language) ||
      ""
  );
  window.customCards.push({
    type: "ajax-doorbell-card",
    name: "Ajax DoorBell",
    description: french
      ? "Interphone pour la sonnette vidéo Ajax : direct, écoute, parole, appel entrant, décrocher et raccrocher."
      : "Intercom for the Ajax video doorbell: live video, listen, talk, incoming call, answer and hang up.",
    preview: true,
    documentationURL: "https://github.com/msdevio/ajax_doorbell",
  });
}

debug(`v${CARD_VERSION} loaded`);
