"""Constants for the Ajax DoorBell & Cameras integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN: Final = "ajax_doorbell"
MANUFACTURER: Final = "Ajax Systems"

CONF_RING_DURATION: Final = "ring_duration"
CONF_CALL_TIMEOUT: Final = "call_timeout"
CONF_NVR_HOST: Final = "nvr_host"
CONF_NVR_PORT: Final = "nvr_port"
CONF_NVR_USERNAME: Final = "nvr_username"
CONF_NVR_PASSWORD: Final = "nvr_password"
CONF_VISIT_RETENTION: Final = "visit_retention"
# What a config entry is: a DoorBell (no kind, as in every earlier version) or an NVR.
CONF_KIND: Final = "kind"
KIND_NVR: Final = "nvr"

DEFAULT_ONVIF_PORT: Final = 8080
DEFAULT_RTSP_PORT: Final = 8554
DEFAULT_USERNAME: Final = "homeassistant"
# How long a ring keeps the call "ringing" before it counts as missed.
DEFAULT_RING_DURATION: Final = 30
# Safety net: a call nobody hangs up (closed tab, lost phone) ends after this.
DEFAULT_CALL_TIMEOUT: Final = 300
# Days of visits (rings, people) listed for replay from the NVR.
DEFAULT_VISIT_RETENTION: Final = 2

# IEEE OUI of Ajax Systems; the hub shares it, so discovery checks for ONVIF.
AJAX_OUI: Final = "9c756e"

# Fired on the bus for every call transition: ring, answered, ended, missed, dismissed.
EVENT_CALL: Final = f"{DOMAIN}_call"

# ONVIF topics of Ajax video devices, without namespace prefixes, matched on
# whole trailing segments: "MotionDetector/Detection" is not PirMotionDetector.
TOPIC_RING: Final = ("RingDetector/Detection",)
TOPIC_PIR: Final = ("PirMotionDetector/Detection",)
TOPIC_MOTION: Final = ("MotionDetector/Detection", "VideoSource/MotionAlarm")
TOPIC_OBJECT: Final = ("ObjectDetection/Object", "ObjectDetector/Detection")
# Ajax's live object tracking (boxes, about five messages a second) shares the
# ObjectDetector topic; it carries no ClassTypes and is left out of subscriptions.
TOPIC_TRACKING: Final = ("ObjectDetector/Detection",)

# The DoorBell clears AI detections itself; this only covers a lost "clear".
DETECTION_HOLD: Final = 300
# The NVR's recording job of a camera: "Active" while it records.
TOPIC_JOB: Final = ("RecordingConfig/JobState",)

# How a camera is recorded, as the NVR's recordings show it.
MODE_CONTINUOUS: Final = "continuous"
MODE_CLIPS: Final = "clips"
MODE_UNKNOWN: Final = "unknown"
MODE_NOT_RECORDED: Final = "not_recorded"

# Two ring messages closer than this are one press.
RING_DEBOUNCE: Final = 1.5

# Codecs go2rtc can feed to a backchannel without transcoding.
TALK_CODECS: Final = frozenset({"PCMA", "PCMU", "OPUS"})


def is_nvr_entry(entry: ConfigEntry) -> bool:
    """Return True for an NVR's config entry, False for a DoorBell's."""
    return entry.data.get(CONF_KIND) == KIND_NVR
