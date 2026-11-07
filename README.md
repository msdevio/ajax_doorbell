# Ajax DoorBell for Home Assistant

[Français](README.fr.md)

A local intercom for the [Ajax DoorBell](https://ajax.systems/products/doorbell/): live
video with near-zero latency, listen and talk to the visitor, a ring event within a
second, a shared call state across every screen, an intercom card and a notification
with an **Answer** button.

Everything stays on your network. The integration talks to the DoorBell over its
official ONVIF and RTSP interface: no Ajax cloud, no Ajax account password.

## Features

- **Live video** through Home Assistant's built-in go2rtc over WebRTC. The H.264
  stream is copied as is, never re-encoded: about 0.3 s glass to glass on the LAN.
- **Listen**: the DoorBell's G.722 audio reaches the browser as Opus.
- **Talk**: the browser microphone goes to the DoorBell speaker through its ONVIF
  audio backchannel. The DoorBell only takes G.722 there, which go2rtc cannot make
  from a browser, so the intercom card encodes the microphone itself (bit-exact with
  FFmpeg's encoder) and the integration plays it on the backchannel, over Home
  Assistant's own websocket: no extra port, and it works remotely too. One screen
  talks at a time; the last one to answer takes over.
- **Ring within half a second** (50 to 500 ms measured) from the DoorBell's local
  ONVIF event (`RingDetector`), as an `event` entity of the `doorbell` class.
- **Call state shared by all screens**: idle, ringing, in call. Answering on one
  screen stops the ringing everywhere; unanswered rings are counted as missed calls.
- **Detections**: camera motion, PIR motion, and person, vehicle and animal from the
  DoorBell's AI. The subscription asks only for these topics, which keeps the
  DoorBell's live object tracking (several messages a second) off the network.
- **Recent visits from the Ajax NVR**: rings and people at the door are listed in the
  card for the last days, and replayed (video and sound) straight from the NVR, from
  5 s before. Nothing is stored in Home Assistant: only the times are kept.
- **Intercom card** `custom:ajax-doorbell-card` and a **notification blueprint**
  (Answer, View, Ignore, Open the door).
- DHCP discovery, reauthentication, reconfiguration, diagnostics, English and French.

## Requirements

- Ajax DoorBell with firmware 2.377 or later (ONVIF support).
- Home Assistant 2026.10 or later.
- HTTPS to use the microphone in a browser or in the companion app.

## 1. Prepare the DoorBell in the Ajax app

1. **Devices → DoorBell → Settings → Service → Connection via ONVIF**, enable
   **Allow ONVIF integration** with the default settings (ONVIF port 8080, RTSP 8554).
2. **ONVIF users**: add a user for Home Assistant (for example `homeassistant`) with
   the **Operator** role and a long password. Ajax allows five ONVIF users per device.
3. Recommended stream settings: a GOP equal to the frame rate (one keyframe per
   second) so the video starts instantly, and 2 to 3 Mbit/s on the main stream, as the
   DoorBell uses 2.4 GHz Wi-Fi.
4. Give the DoorBell a fixed address in your router (DHCP reservation).

## 2. Install the integration

Add this repository as a custom repository (Marketplace or HACS, category
**Integration**), install **Ajax DoorBell**, then restart Home Assistant. Or copy
`custom_components/ajax_doorbell` into your `config/custom_components/` folder.

Home Assistant discovers the DoorBell on your network. Otherwise add it from
**Settings → Devices & services → Add integration → Ajax DoorBell**, with its IP
address and the ONVIF user.

## Entities

| Entity | Description |
| --- | --- |
| `camera.<doorbell>` | Main stream (up to 2688×1520), WebRTC through go2rtc, two-way audio |
| `camera.<doorbell>_substream` | Lighter stream for small tiles (disabled by default) |
| `event.<doorbell>_doorbell` | Fires `ring` on every press |
| `binary_sensor.<doorbell>_incoming_call` | On while it rings |
| `sensor.<doorbell>_call` | `idle`, `ringing` or `in_call`, with `answered_by` and times |
| `sensor.<doorbell>_last_ring` | Time of the last press |
| `sensor.<doorbell>_missed_calls` | Unanswered rings since the counter was cleared |
| `binary_sensor.<doorbell>_motion` | Motion seen by the camera |
| `binary_sensor.<doorbell>_pir_motion` | Motion seen by the PIR sensor |
| `binary_sensor.<doorbell>_person`, `_vehicle`, `_animal` | AI detections |
| `binary_sensor.<doorbell>_event_stream` | Health of the ONVIF event subscription (diagnostic) |
| `camera.<doorbell>_replay` | The NVR recording the card replays (hidden; only with an NVR) |

## Services

| Service | Effect |
| --- | --- |
| `ajax_doorbell.answer` | Marks the call answered (`answered_by` optional); calling it again keeps the call alive |
| `ajax_doorbell.hang_up` | Ends the call |
| `ajax_doorbell.dismiss` | Stops the ringing without answering |
| `ajax_doorbell.clear_missed` | Resets the missed call counter |

Target a DoorBell entity or device; without a target the service acts on every DoorBell.

## Events

`ajax_doorbell_call` is fired on every call change, with `type` (`ring`, `answered`,
`ended`, `missed`, `dismissed`, `missed_cleared`), `state`, `device_id` and
`config_entry_id`, plus `answered_by`, `after_ring`, `during_call` or `reason` when
relevant.

## Intercom card

The integration serves the card and loads it on every dashboard: there is nothing
to copy and no resource to add, and each update reaches the browsers at once. Add
the card to a view (a dedicated "Intercom" view works best); the visual editor covers
every option, or use YAML:

```yaml
type: custom:ajax-doorbell-card
camera: camera.ajax_doorbell             # required
call_entity: sensor.ajax_doorbell_call   # optional, found on the camera's device
name: Front door                         # default: the camera's name
answered_by: Hall tablet                 # default: "<user> (phone|tablet|computer)"
two_way_audio: true                      # true forces talk-back, false listen only
push_to_talk: false                      # hold the button to talk
preview_on_ring: true                    # start the video muted as soon as it rings
ring_sound: false                        # ringtone in the browser
ring_tone: chime                         # 22 tones: chime, classic, westminster, harp, zen, bird…
ring_volume: 55                          # 0 to 100
ring_tone_url: /local/ringtone.mp3       # optional: your own file in config/www/
lock: lock.front_door                    # optional: an "Open" button (any lock, e.g. Nuki)
lock_action: open                        # open (pull the latch) or unlock
lock_confirm: true                       # press twice to open
fullscreen_on_ring: false
show_stats: false                        # RTT, jitter, resolution, codecs
auto_retry: true
aspect_ratio: "16 / 9"
```

When it rings the card shows an incoming-call screen with **Answer**, **View** and
**Ignore**. **View** then **Answer** keeps the same connection: only the microphone
is added. Answering on one screen shows "Answered on …" on the others. Opening the
view with `?ajax_answer=1` (or `?ajax_answer=camera.ajax_doorbell` when there are
several doorbells) answers at once: that is what the notification button uses.

## Recordings from the Ajax NVR

If an Ajax NVR records the DoorBell, open the integration's **Configure** and enter
the NVR's address. It needs ONVIF on the NVR (Ajax app, NVR settings) with the same
ONVIF user as the DoorBell, or another one given in the options.

The card then shows **Recent visits**: each ring and each person detected at the
door, for the number of days you choose (2 by default), with a picture. Tap one to
replay it from 5 s before, with sound; **−10 s** and **+10 s** move in the
recording, **Live** goes back to the doorbell.

Nothing is recorded or stored by Home Assistant: the video stays on the NVR. A small
relay inside the integration, on `127.0.0.1` only, lets Home Assistant's go2rtc play
the NVR's recording, and pictures are taken from it when shown and kept in memory.
This needs Home Assistant's built-in go2rtc (the default).

## Notification with an Answer button

Import the blueprint
[`blueprints/automation/ajax_doorbell/doorbell_notify.yaml`](blueprints/automation/ajax_doorbell/doorbell_notify.yaml)
(**Settings → Automations → Blueprints → Import**). When someone rings, the chosen
phones get the picture of the door with **Answer**, **View** and **Ignore**, plus
**Open the door** if you pick a lock. **Answer** opens your intercom view, whose card
takes the call at once. The notification disappears from every phone as soon as
someone answers; a missed call can leave a notification instead. On Android it can
also open the intercom view by itself, like an incoming call.

## Troubleshooting

- **No microphone**: the page must be served over HTTPS (or `http://localhost`).
- **No Talk button or no sound at the door**: the call sensor must show
  `two_way_audio: true` and `talk_via: relay`, and the diagnostics a `talk_codec`.
  Home Assistant's own Talk button stays hidden for this DoorBell: its backchannel is
  G.722, so talking goes through the intercom card.
- **"Another screen took the microphone"**: only one screen talks at a time, the last
  one to answer.
- **Higher latency**: do not set a camera orientation in Home Assistant (it forces
  the video to be re-encoded), and prefer the main stream over HLS players. The
  DoorBell sends a keyframe about 130 ms after a new connection, so the video starts
  fastest with **Preload stream** off; with it on, a 1 s GOP keeps the start short.
- **Remote access on 4G**: media flows directly over WebRTC. Behind a strict mobile
  NAT, declare a TURN server under `web_rtc: ice_servers:` in `configuration.yaml`.
- **Event stream offline**: the ONVIF user may lack the rights to subscribe to events:
  give it the Operator role.

## How it works

```
DoorBell ── RTSP 8554 (H.264, G.722) ──► go2rtc ── WebRTC ──► browser (video, listening)
browser ── websocket (G.722 frames) ──► ajax_doorbell ── RTSP backchannel ──► DoorBell speaker
DoorBell ── ONVIF 8080 (PullPoint events) ──► ajax_doorbell ──► entities, call state, notification
```

The Ajax app keeps working as before: it uses its own encrypted channel, the ONVIF
access is an independent, local door.

## Privacy and security

The ONVIF account bypasses the Ajax cloud, so treat it like a key: a dedicated user, a
long password, and no DoorBell port open to the Internet. Credentials stay in Home
Assistant's configuration; diagnostics and logs never contain them.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with Ajax Systems.
