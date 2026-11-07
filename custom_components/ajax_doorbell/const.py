"""Constants for the Ajax DoorBell integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ajax_doorbell"
MANUFACTURER: Final = "Ajax Systems"

CONF_RTSP_PORT: Final = "rtsp_port"
CONF_RING_DURATION: Final = "ring_duration"
CONF_CALL_TIMEOUT: Final = "call_timeout"

DEFAULT_ONVIF_PORT: Final = 8080
DEFAULT_RTSP_PORT: Final = 8554
DEFAULT_USERNAME: Final = "homeassistant"
# How long a ring keeps the call "ringing" before it counts as missed.
DEFAULT_RING_DURATION: Final = 30
# Safety net: a call nobody hangs up (closed tab, lost phone) ends after this.
DEFAULT_CALL_TIMEOUT: Final = 300

# IEEE OUI of Ajax Systems; the hub shares it, so discovery checks for ONVIF.
AJAX_OUI: Final = "9c756e"

# Fired on the bus for every call transition: ring, answered, ended, missed, dismissed.
EVENT_CALL: Final = f"{DOMAIN}_call"

# ONVIF topics of Ajax video devices, matched as substrings so the namespace
# prefix the device picks does not matter.
TOPIC_RING: Final = ("RingDetector/Detection",)
TOPIC_MOTION: Final = ("MotionDetector/Detection", "VideoSource/MotionAlarm")
TOPIC_OBJECT: Final = ("ObjectDetector/Detection", "ObjectDetection/Object")

# A detection the device never clears turns off after this many seconds.
DETECTION_HOLD: Final = 30
# Two ring messages closer than this are one press.
RING_DEBOUNCE: Final = 1.5

# Codecs go2rtc can feed to a backchannel without transcoding.
TALK_CODECS: Final = frozenset({"PCMA", "PCMU", "OPUS"})
