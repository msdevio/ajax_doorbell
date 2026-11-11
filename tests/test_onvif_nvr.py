"""Tests for the ONVIF calls only the NVR entry makes."""

from __future__ import annotations

from datetime import UTC, datetime
import xml.etree.ElementTree as ET

from custom_components.ajax_doorbell.onvif import AjaxOnvifClient, ProfileDetails

from .conftest import HOST
from .test_onvif import FakeSession

ENV = (
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
    'xmlns:trc="http://www.onvif.org/ver10/recording/wsdl" '
    'xmlns:tse="http://www.onvif.org/ver10/search/wsdl" '
    'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"><s:Body>{}</s:Body></s:Envelope>'
)


def _reply(body: str) -> tuple[int, bytes]:
    return 200, ENV.format(body).encode()


async def test_profile_details() -> None:
    """A profile's own name (not a configuration's) and its video source."""
    session = FakeSession(
        [
            _reply(
                "<tr2:GetProfilesResponse>"
                '<tr2:Profiles token="cam-0-main"><tr2:Configurations>'
                '<tr2:VideoSource token="vsc-1"><tt:Name>camera porte</tt:Name>'
                "<tt:SourceToken>cam-0-v</tt:SourceToken></tr2:VideoSource>"
                "</tr2:Configurations><tr2:Name>camera garage _main</tr2:Name></tr2:Profiles>"
                '<tr2:Profiles token="other-0-main"><tr2:Configurations/></tr2:Profiles>'
                "</tr2:GetProfilesResponse>"
            )
        ]
    )
    client = AjaxOnvifClient(session, HOST, 8080, "u", "p")
    assert await client.async_get_profile_details() == {
        "cam-0-main": ProfileDetails(
            "camera garage _main", frozenset({"vsc-1", "cam-0-v"}), "camera porte"
        ),
        "other-0-main": ProfileDetails("", frozenset(), ""),
    }


async def test_recording_jobs() -> None:
    """Recording jobs map to the recording they fill."""
    session = FakeSession(
        [
            _reply(
                "<trc:GetRecordingJobsResponse><trc:JobItem><tt:JobToken>job_7</tt:JobToken>"
                "<tt:JobConfiguration><tt:RecordingToken>cam-0-main-r</tt:RecordingToken>"
                "<tt:Mode>Active</tt:Mode></tt:JobConfiguration></trc:JobItem>"
                "</trc:GetRecordingJobsResponse>"
            )
        ]
    )
    client = AjaxOnvifClient(session, HOST, 8080, "u", "p")
    client.services["recording"] = f"http://{HOST}:8080/onvif/recording_service"
    assert await client.async_get_recording_jobs() == {"job_7": "cam-0-main-r"}


def _result(moment: str, value: str, start: bool = False) -> str:
    return (
        f"<tt:Result><tt:RecordingToken>cam-0-main-r</tt:RecordingToken><tt:Time>{moment}"
        "</tt:Time><tt:Event><wsnt:NotificationMessage><wsnt:Topic>"
        "tns1:RecordingHistory/Recording/State</wsnt:Topic><wsnt:Message><tt:Message>"
        f'<tt:Data><tt:SimpleItem Name="IsRecording" Value="{value}"/></tt:Data>'
        "</tt:Message></wsnt:Message></wsnt:NotificationMessage></tt:Event>"
        f"<tt:StartStateEvent>{'true' if start else 'false'}</tt:StartStateEvent></tt:Result>"
    )


async def test_recording_history() -> None:
    """When a recording held video, from the NVR's event search."""
    session = FakeSession(
        [
            _reply(
                "<tse:FindEventsResponse><tse:SearchToken>s1</tse:SearchToken></tse:FindEventsResponse>"
            ),
            _reply(
                "<tse:GetEventSearchResultsResponse><tse:ResultList>"
                "<tt:SearchState>Completed</tt:SearchState>"
                + _result("2026-11-11T10:05:00Z", "false")
                + _result("2026-11-11T10:00:00Z", "true", start=True)
                + _result("2026-11-11T10:20:00Z", "true")
                + "</tse:ResultList></tse:GetEventSearchResultsResponse>"
            ),
            _reply("<tse:EndSearchResponse/>"),
        ]
    )
    client = AjaxOnvifClient(session, HOST, 8080, "u", "p")
    client.services["search"] = f"http://{HOST}:8080/onvif/search_service"
    history = await client.async_find_recording_history(
        "cam-0-main-r",
        datetime(2026, 11, 11, 4, tzinfo=UTC),
        datetime(2026, 11, 11, 10, 30, tzinfo=UTC),
    )
    assert history == [
        (datetime(2026, 11, 11, 10, 0, tzinfo=UTC), True),
        (datetime(2026, 11, 11, 10, 5, tzinfo=UTC), False),
        (datetime(2026, 11, 11, 10, 20, tzinfo=UTC), True),
    ]
    request = session.calls[0]["data"].decode()
    ET.fromstring(request)  # well-formed, every prefix declared
    assert "<tt:IncludedRecordings>cam-0-main-r</tt:IncludedRecordings>" in request
    assert "tns1:RecordingHistory//.</wsnt:TopicExpression>" in request
    assert "<StartPoint>2026-11-11T04:00:00Z</StartPoint>" in request
    assert "<SearchToken>s1</SearchToken>" in session.calls[2]["data"].decode()  # search ended
