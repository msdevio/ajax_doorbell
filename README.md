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
- **Talk**: the browser microphone goes back to the DoorBell speaker through the
  ONVIF audio backchannel. The integration checks at setup that the DoorBell offers
  one; on Home Assistant 2026.11 the camera then declares two-way audio, so Home
  Assistant's own **Talk** button (Labs preview) works too.
- **Ring within a second** from the DoorBell's local ONVIF event (`RingDetector`),
  as an `event` entity of the `doorbell` class.
- **Call state shared by all screens**: idle, ringing, in call. Answering on one
  screen stops the ringing everywhere; unanswered rings are counted as missed calls.
- **Detections**: motion, person, vehicle and animal from the DoorBell's AI.
- **Intercom card** `custom:ajax-doorbell-card` and a **notification blueprint**
  (Answer, View, Ignore, Open the door).
- DHCP discovery, reauthentication, reconfiguration, diagnostics, English and French.

## Requirements

- Ajax DoorBell with firmware 2.377 or later (ONVIF support).
- Home Assistant 2026.10 or later. 2026.11 adds Home Assistant's own Talk button.
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
| `binary_sensor.<doorbell>_motion`, `_person`, `_vehicle`, `_animal` | Detections |
| `binary_sensor.<doorbell>_event_stream` | Health of the ONVIF event subscription (diagnostic) |

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
- **No Talk button or no sound at the door**: check the `two_way_audio` attribute of
  the call sensor, and the backchannel in the diagnostics. Talk needs the DoorBell to
  offer a G.711 or Opus backchannel.
- **Higher latency**: do not set a camera orientation in Home Assistant (it forces
  the video to be re-encoded), and prefer the main stream over HLS players.
- **Remote access on 4G**: media flows directly over WebRTC. Behind a strict mobile
  NAT, declare a TURN server under `web_rtc: ice_servers:` in `configuration.yaml`.
- **Event stream offline**: the ONVIF user may lack the rights to subscribe to events:
  give it the Operator role.

## How it works

```
DoorBell ── RTSP 8554 (H.264, G.722, backchannel) ──► go2rtc ── WebRTC ──► browser
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
