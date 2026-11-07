"""Tests for the ONVIF client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest

from custom_components.ajax_doorbell.onvif import (
    AjaxOnvifClient,
    OnvifAuthError,
    OnvifConnectionError,
    OnvifError,
    PullPoint,
    host_port,
    parse_notifications,
    parse_profiles,
    parse_topics,
)

from .conftest import HOST, load_xml

DEVICE_INFO = (
    b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/'
    b'device/wsdl"><tds:Manufacturer>AJAX</tds:Manufacturer><tds:Model>oa8000-nand'
    b"</tds:Model><tds:FirmwareVersion>2.669-1</tds:FirmwareVersion><tds:SerialNumber>"
    b"AJX12345</tds:SerialNumber></tds:GetDeviceInformationResponse></s:Body></s:Envelope>"
)
NOT_AUTHORIZED = (
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><s:Fault>'
    b"<s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value>"
    b"</s:Subcode></s:Code><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason>"
    b"</s:Fault></s:Body></s:Envelope>"
)


def _date_time(moment: datetime) -> bytes:
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://'
        'www.onvif.org/ver10/schema"><s:Body><GetSystemDateAndTimeResponse><tt:'
        "SystemDateAndTime><tt:UTCDateTime><tt:Time>"
        f"<tt:Hour>{moment.hour}</tt:Hour><tt:Minute>{moment.minute}</tt:Minute>"
        f"<tt:Second>{moment.second}</tt:Second></tt:Time><tt:Date>"
        f"<tt:Year>{moment.year}</tt:Year><tt:Month>{moment.month}</tt:Month>"
        f"<tt:Day>{moment.day}</tt:Day></tt:Date></tt:UTCDateTime></tt:SystemDateAndTime>"
        "</GetSystemDateAndTimeResponse></s:Body></s:Envelope>"
    ).encode()


class FakeResponse:
    """A canned aiohttp response."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class FakeSession:
    """Replies in order and records what was sent."""

    def __init__(self, replies: list[tuple[int, bytes]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        status, body = self.replies.pop(0)
        return FakeResponse(status, body)


def test_parse_profiles() -> None:
    """Media2 profiles keep what the integration needs."""
    main, sub = parse_profiles(load_xml("get_profiles.xml"))
    assert (main.token, main.encoding, main.width, main.height) == (
        "main_token",
        "H264",
        2688,
        1520,
    )
    assert (main.fps, main.bitrate, main.gov_length) == (25.0, 2048, 25)
    assert (main.audio_encoding, main.audio_sample_rate, main.audio_output) == (
        "G722",
        16,
        True,
    )
    assert (sub.width, sub.height, sub.audio_encoding, sub.audio_output) == (
        1024,
        576,
        None,
        False,
    )
    assert main.pixels > sub.pixels


def test_parse_topics() -> None:
    """The topic set flattens with the Ajax prefixes."""
    assert parse_topics(load_xml("get_event_properties.xml")) == [
        "tns1:RuleEngine/RingDetector/Detection",
        "tns1:RuleEngine/tnsajax:MotionDetector/Detection",
        "tns1:RuleEngine/tnsajax:ObjectDetector/Detection",
    ]


def test_parse_notifications() -> None:
    """A ring and an AI detection come out of one PullMessages answer."""
    ring, objects = parse_notifications(load_xml("pull_messages_ring.xml"))
    assert ring.topic == "tns1:RuleEngine/RingDetector/Detection"
    assert ring.operation == "Changed"
    assert ring.data == {"Detected": "true"}
    assert ring.source == {"VideoSourceConfigurationToken": "vs"}
    assert ring.utc_time == datetime(2026, 11, 7, 10, 0, 0, 250000, tzinfo=UTC)
    assert objects.data == {"ClassTypes": "Human,Animal"}


async def test_switches_to_http_digest() -> None:
    """A 401 makes the client retry, and stay, with HTTP Digest."""
    session = FakeSession([(401, b""), (200, DEVICE_INFO), (200, DEVICE_INFO)])
    client = AjaxOnvifClient(session, HOST, 8080, "homeassistant", "secret")
    identity = await client.async_get_identity()
    assert (identity.model, identity.firmware, identity.serial) == (
        "oa8000-nand",
        "2.669-1",
        "AJX12345",
    )
    assert client.uses_http_digest
    assert session.calls[0]["middlewares"] == ()
    assert len(session.calls[1]["middlewares"]) == 1
    assert b"UsernameToken" in session.calls[0]["data"]
    await client.async_get_identity()
    assert len(session.calls[2]["middlewares"]) == 1


@pytest.mark.parametrize(
    ("replies"),
    [[(401, b""), (401, b"")], [(400, NOT_AUTHORIZED), (400, NOT_AUTHORIZED)]],
)
async def test_auth_error(replies: list[tuple[int, bytes]]) -> None:
    """Refused twice, with and without HTTP Digest, is an auth error."""
    client = AjaxOnvifClient(FakeSession(replies), HOST, 8080, "homeassistant", "bad")
    with pytest.raises(OnvifAuthError):
        await client.async_get_identity()


async def test_soap_fault_is_an_error() -> None:
    """Another fault is reported with its reason."""
    fault = NOT_AUTHORIZED.replace(b"ter:NotAuthorized", b"ter:ActionNotSupported")
    client = AjaxOnvifClient(FakeSession([(500, fault)]), HOST, 8080, "u", "p")
    with pytest.raises(OnvifError, match="ActionNotSupported"):
        await client.async_get_identity()


async def test_connection_error() -> None:
    """Network failures are connection errors."""

    class Down:
        def post(self, url: str, **kwargs: Any) -> None:
            raise aiohttp.ClientConnectionError("down")

    client = AjaxOnvifClient(Down(), HOST, 8080, "u", "p")
    with pytest.raises(OnvifConnectionError):
        await client.async_get_identity()


async def test_time_offset() -> None:
    """The device clock is read without credentials."""
    ahead = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    session = FakeSession([(200, _date_time(ahead))])
    client = AjaxOnvifClient(session, HOST, 8080, "u", "p")
    offset = await client.async_update_time_offset()
    assert timedelta(minutes=4, seconds=55) < offset <= timedelta(minutes=5)
    assert b"UsernameToken" not in session.calls[0]["data"]


def test_same_host_keeps_the_port() -> None:
    """Device-reported addresses are aimed at the configured host."""
    client = AjaxOnvifClient(FakeSession([]), "10.0.0.5", 8080, "u", "p")
    assert (
        client._same_host("http://192.168.1.2:8080/onvif/event_service?sub=1")
        == "http://10.0.0.5:8080/onvif/event_service?sub=1"
    )
    assert client._same_host("http://192.168.1.2/snap.jpg") == "http://10.0.0.5/snap.jpg"


def test_naive_times_are_utc() -> None:
    """A device that leaves out the Z still gives an aware time."""
    xml = load_xml("pull_messages_ring.xml")
    for message in xml.iter():
        if message.get("UtcTime"):
            message.set("UtcTime", message.get("UtcTime").rstrip("Z"))
    ring = parse_notifications(xml)[0]
    assert ring.utc_time == datetime(2026, 11, 7, 10, 0, 0, 250000, tzinfo=UTC)


async def test_never_follows_redirects() -> None:
    """Requests skip the session middlewares, so they must not follow redirects."""
    session = FakeSession([(200, DEVICE_INFO)])
    await AjaxOnvifClient(session, HOST, 8080, "u", "p").async_get_identity()
    assert session.calls[0]["allow_redirects"] is False


async def test_colon_in_username_is_an_auth_error() -> None:
    """HTTP Digest cannot carry a ':' in the username."""
    client = AjaxOnvifClient(FakeSession([(401, b"")]), HOST, 8080, "a:b", "p")
    with pytest.raises(OnvifAuthError):
        await client.async_get_identity()


async def test_reference_parameters_are_echoed() -> None:
    """Subscription calls carry the reference parameters the device gave."""
    created = (
        b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://'
        b'www.w3.org/2005/08/addressing" xmlns:tev="http://www.onvif.org/ver10/events/wsdl">'
        b"<s:Body><tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
        b"<wsa:Address>http://192.168.1.9:8080/onvif/event_service?sub=7</wsa:Address>"
        b'<wsa:ReferenceParameters><dn:SubId xmlns:dn="urn:x">7</dn:SubId>'
        b"</wsa:ReferenceParameters></tev:SubscriptionReference>"
        b"</tev:CreatePullPointSubscriptionResponse></s:Body></s:Envelope>"
    )
    empty = b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>'
    session = FakeSession([(200, created), (200, empty)])
    client = AjaxOnvifClient(session, HOST, 8080, "u", "p")
    pullpoint = await client.async_create_pullpoint(600)
    assert pullpoint.address == f"http://{HOST}:8080/onvif/event_service?sub=7"
    assert "SubId" in pullpoint.reference_parameters
    assert await client.async_pull(pullpoint, 1) == []
    assert b"SubId" in session.calls[1]["data"]
    assert isinstance(pullpoint, PullPoint)


def test_host_port_brackets_ipv6() -> None:
    """IPv6 literals get brackets in URLs."""
    assert host_port("fe80::1", 8554) == "[fe80::1]:8554"
    assert host_port("192.168.1.2", 8554) == "192.168.1.2:8554"
    assert host_port("doorbell.lan", None) == "doorbell.lan"
