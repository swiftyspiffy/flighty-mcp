from __future__ import annotations

import asyncio
import gzip
import json

import httpx
import pytest
import respx

from flighty_mcp import proto
from flighty_mcp.capabilities import capability_header, client_capabilities
from flighty_mcp.client import FlightyClient
from flighty_mcp.config import API_BASE, Settings
from flighty_mcp.endpoints import ENDPOINTS
from flighty_mcp.errors import FlightyError, WritesDisabled

from .conftest import (
    FLIGHT,
    OTHER,
    OTHER_TOKEN,
    OWNER,
    SYNC,
    TOKEN,
    fake_token,
    flight_record,
    mock_sync,
    sync_body,
)


async def test_full_sync_sends_no_body_and_matches_app_headers(client: FlightyClient) -> None:
    route = mock_sync()
    snapshot = await client.snapshot(TOKEN)
    request = route.calls[0].request
    assert request.method == "POST"
    assert request.content == b""
    assert request.headers["Authorization"] == "Bearer " + TOKEN
    assert request.headers["X-Flighty-Build-Token"] == "synthetic-build-credential"
    assert request.headers["User-Agent"] == "Flighty 4.12.0 (4875) com.flightyapp.flighty"
    assert request.headers["X-Flighty-Locale"] == "en_US"
    assert request.headers["Accept"] == request.headers["Content-Type"] == "application/x-protobuf"
    assert request.headers["Accept-Encoding"] == "gzip"
    assert "Device" not in request.headers
    assert snapshot.account_id == OWNER
    assert snapshot.items("remoteUser")[0]["token"] == "[REDACTED]"


async def test_pagination_uses_only_query_never_server_supplied_host(client: FlightyClient) -> None:
    second = respx.post(SYNC, params={"cursor": "two"}).respond(
        200, content=sync_body(), headers={"Content-Type": "application/x-protobuf"}
    )
    first = respx.post(SYNC).respond(
        200,
        content=sync_body(more=True, cursor="https://untrusted.invalid/unrelated?cursor=two"),
        headers={"Content-Type": "application/x-protobuf"},
    )
    result = await client.snapshot(TOKEN)
    assert result.pages == 2
    assert first.called and second.called
    assert all(call.request.url.host == "api.flightyapp.com" for call in respx.calls)
    assert all(call.request.content == b"" for call in respx.calls)


@pytest.mark.parametrize(
    "cursor", [None, "https://api.flightyapp.com/v1/sync/full", "?cursor=loop"]
)
async def test_bad_pagination_does_not_cache_partial_data(
    client: FlightyClient, cursor: str | None
) -> None:
    mock_sync(sync_body(more=True, cursor=cursor))
    with pytest.raises(FlightyError, match="pagination|cursor"):
        await client.snapshot(TOKEN)
    assert not client._cache


async def test_page_limit_is_enforced() -> None:
    client = FlightyClient(Settings(max_pages=1))
    mock_sync(sync_body(more=True, cursor="?cursor=two"))
    with pytest.raises(FlightyError, match="max-pages"):
        await client.snapshot(TOKEN)
    assert not client._cache


async def test_same_flight_different_owners_not_overwritten(client: FlightyClient) -> None:
    mock_sync(sync_body([flight_record(), flight_record(OTHER)]))
    snapshot = await client.snapshot(TOKEN)
    assert len(snapshot.items("remoteUserFlight")) == 2
    assert len(snapshot.owned("remoteUserFlight")) == 1


async def test_newer_deletion_marker_wins(client: FlightyClient) -> None:
    newer = flight_record(updated="2000-01-03T00:00:00Z", deleted="2000-01-03T00:00:00Z")
    mock_sync(sync_body([newer, flight_record()]))
    snapshot = await client.snapshot(TOKEN)
    assert not snapshot.owned("remoteUserFlight")
    assert len(snapshot.owned("remoteUserFlight", include_deleted=True)) == 1


async def test_cache_isolated_by_credential_not_claimed_subject(client: FlightyClient) -> None:
    route = mock_sync()
    await client.snapshot(TOKEN)
    await client.snapshot(fake_token(OWNER, "different-credential"))
    assert route.call_count == 2
    assert len(client._cache) == 2
    assert all(TOKEN not in key and OWNER not in key for key in client._cache)


async def test_cache_is_deep_copied_and_refreshable(client: FlightyClient) -> None:
    route = mock_sync()
    first = await client.snapshot(TOKEN)
    first.records.clear()
    assert (await client.snapshot(TOKEN)).records
    assert route.call_count == 1
    refreshed = await client.snapshot(TOKEN, refresh=True)
    assert refreshed.refresh_limited
    assert route.call_count == 1
    for snapshot in client._cache.values():
        snapshot.stored_at -= 61
    assert not (await client.snapshot(TOKEN, refresh=True)).refresh_limited
    assert route.call_count == 2


async def test_parallel_reads_coalesce(client: FlightyClient) -> None:
    route = mock_sync()
    await asyncio.gather(*(client.snapshot(TOKEN) for _ in range(5)))
    assert route.call_count == 1


async def test_bounded_cache_evicts_oldest() -> None:
    client = FlightyClient(Settings(max_cache_accounts=1))
    mock_sync()
    await client.snapshot(TOKEN)
    await client.snapshot(OTHER_TOKEN)
    assert len(client._cache) == 1


@pytest.mark.parametrize(
    "operation", [name for name, endpoint in ENDPOINTS.items() if not endpoint.read_only]
)
async def test_every_mutating_route_disabled_before_http(
    client: FlightyClient, operation: str
) -> None:
    with pytest.raises(WritesDisabled):
        await client.request(operation, TOKEN)
    assert not respx.calls


async def test_read_only_sync_cannot_accept_a_change_body(client: FlightyClient) -> None:
    with pytest.raises(FlightyError, match="must not contain"):
        await client.request("sync_account", TOKEN, payload={"changes": []})
    assert not respx.calls


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 404, 429, 500, 503])
async def test_errors_never_retried_or_echoed(client: FlightyClient, status: int) -> None:
    route = respx.get(API_BASE + "/v1/radar/layers").respond(
        status,
        content=b"PRIVATE RESPONSE MUST NOT LEAK",
        headers={"Location": "https://untrusted.invalid", "Retry-After": "30"},
    )
    with pytest.raises(FlightyError) as error:
        await client.request("get_radar_layers", TOKEN)
    assert "PRIVATE RESPONSE" not in str(error.value)
    assert TOKEN not in str(error.value)
    assert route.call_count == 1


async def test_network_exception_hides_details(client: FlightyClient) -> None:
    respx.get(API_BASE + "/v1/radar/layers").mock(
        side_effect=httpx.ConnectError("synthetic-secret")
    )
    with pytest.raises(FlightyError) as error:
        await client.request("get_radar_layers", TOKEN)
    assert "synthetic-secret" not in str(error.value)


async def test_gzip_response(client: FlightyClient) -> None:
    raw = proto.encode("GetRadarLayersResponseProto", {"layers": []})
    respx.get(API_BASE + "/v1/radar/layers").respond(
        200,
        content=gzip.compress(raw),
        headers={"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"},
    )
    assert (await client.request("get_radar_layers", TOKEN))["data"] == {}


async def test_response_size_limit() -> None:
    client = FlightyClient(Settings(max_response_bytes=2))
    respx.get(API_BASE + "/v1/radar/layers").respond(
        200, content=b"long", headers={"Content-Type": "application/x-protobuf"}
    )
    with pytest.raises(FlightyError, match="size limit"):
        await client.request("get_radar_layers", TOKEN)


async def test_conditional_request_304(client: FlightyClient) -> None:
    route = respx.post(API_BASE + f"/v1/sync/content/flight/{FLIGHT}").respond(304)
    result = await client.request(
        "poll_flight",
        TOKEN,
        path={"flight_id": FLIGHT},
        headers={"If-None-Match": '"example"', "X-Sync-Priority": "10"},
    )
    assert result["not_modified"] is True
    assert route.calls[0].request.content == b""


async def test_rejects_arbitrary_destination_and_headers(client: FlightyClient) -> None:
    for operation, kwargs in [
        ("https://untrusted.invalid", {}),
        ("get_flight", {"path": {"flight_id": "../../user"}}),
        ("get_radar_layers", {"headers": {"Authorization": "wrong"}}),
    ]:
        with pytest.raises(FlightyError):
            await client.request(operation, TOKEN, **kwargs)
    assert not respx.calls


async def test_account_delete_has_separate_gate(writer: FlightyClient) -> None:
    with pytest.raises(WritesDisabled, match="separately"):
        await writer.request("delete_account", TOKEN)
    assert not respx.calls


ACTIONS = [
    f for f in proto.SCHEMA["SyncChangeRequestProto"]["fields"] if f.get("oneof") == "change"
]


@pytest.mark.parametrize("action", [f["json_name"] for f in ACTIONS])
async def test_all_46_change_actions_encode_on_correct_tag(
    writer: FlightyClient, action: str
) -> None:
    route = respx.post(SYNC).respond(
        200, content=b"", headers={"Content-Type": "application/x-protobuf"}
    )
    body = {
        "whenToLeavePlanChangeAction": {"planId": "synthetic-plan", "operation": "REQUEST_REFRESH"},
        "passportSharedAction": {"year": 2099},
        "deviceChangeAction": {"capabilities": client_capabilities()},
    }.get(action, {})
    await writer.apply_changes(
        TOKEN, [{"timestamp": "2000-01-01T00:00:00Z", action: body}], confirm_destructive=True
    )
    wire = route.calls[0].request.content
    decoded = proto.decode("SyncRequestProto", wire)
    assert decoded["changes"][0][action] == body
    number = next(f["number"] for f in ACTIONS if f["json_name"] == action)

    def varint(value: int) -> bytes:
        result = bytearray()
        while value > 127:
            result.append((value & 127) | 128)
            value >>= 7
        return bytes(result + bytes([value]))

    typename = next(f["type"] for f in ACTIONS if f["json_name"] == action)
    encoded_body = proto.encode(typename, body)
    assert wire.endswith(varint(number * 8 + 2) + varint(len(encoded_body)) + encoded_body)
    assert len(ACTIONS) == 46


@pytest.mark.parametrize(
    "changes,confirm",
    [
        ([], False),
        ([{}], False),
        ([{"notAnAction": {}}], False),
        ([{"flightSetMineAction": {}, "flightUnsetMineAction": {}}], False),
        ([{"flightDeleteAction": {"flightId": FLIGHT}}], False),
        ([{"flightDeleteAction": {"flightId": FLIGHT, "ownerUserId": OTHER}}], True),
        ([{"flightSetMineAction": {"flightId": FLIGHT, "unexpected": True}}], False),
    ],
)
async def test_invalid_or_destructive_changes_refused_before_http(
    writer: FlightyClient, changes: list, confirm: bool
) -> None:
    with pytest.raises(FlightyError):
        await writer.apply_changes(TOKEN, changes, confirm_destructive=confirm)
    assert not respx.calls


async def test_successful_write_invalidates_only_own_cache(writer: FlightyClient) -> None:
    route = mock_sync()
    await writer.snapshot(TOKEN)
    await writer.snapshot(OTHER_TOKEN)
    respx.post(API_BASE + f"/v1/flight/{FLIGHT}/subscribe").respond(200)
    await writer.request(
        "subscribe_flight", TOKEN, path={"flight_id": FLIGHT}, params={"is_passenger": "true"}
    )
    assert len(writer._cache) == 1
    await writer.snapshot(OTHER_TOKEN)
    assert route.call_count == 2
    await writer.snapshot(TOKEN)
    assert route.call_count == 3


async def test_empty_mutation_ack_requires_readback_without_retry(writer: FlightyClient) -> None:
    route = respx.post(SYNC).respond(200)
    result = await writer.apply_changes(TOKEN, [{"flightSetMineAction": {"flightId": FLIGHT}}])
    assert result["submitted_changes"] == 1
    assert result["data"] == {"returned_record_counts": {}, "more_data": False}
    assert result["readback_required"] is True
    assert route.call_count == 1


@pytest.mark.parametrize(
    "operation,body,path",
    [
        ("request_export", {"email": "test@example.invalid"}, {}),
        ("request_bulk_import", {"email": "test@example.invalid", "service": "OPEN_FLIGHTS"}, {}),
        ("link_account", {"token": "synthetic-link-value"}, {}),
        (
            "register_device",
            {
                "id": "synthetic-device",
                "appVersion": "4818",
                "pushToken": "synthetic-push",
                "bundleId": "com.flightyapp.flighty",
            },
            {},
        ),
        ("upload_receipt", {"storekit1Receipt": "dGVzdA=="}, {}),
        (
            "start_live_activity",
            {
                "flightId": FLIGHT,
                "userId": OWNER,
                "liveActivityId": "synthetic-activity",
                "bundleId": "com.flightyapp.flighty",
            },
            {"flight_id": FLIGHT},
        ),
    ],
)
async def test_proto_mutation_envelopes_mock_only(
    writer: FlightyClient, operation: str, body: dict, path: dict
) -> None:
    endpoint = ENDPOINTS[operation]
    url = API_BASE + endpoint.path.format(**path)
    route = respx.request(endpoint.method, url).respond(200)
    await writer.request(operation, TOKEN, path=path, payload=body)
    expected = (
        {"capabilities": client_capabilities(), **body} if operation == "register_device" else body
    )
    assert route.calls[0].request.content == proto.encode(endpoint.request, expected)
    assert route.calls[0].request.headers["Content-Type"] == "application/x-protobuf"


async def test_json_mutation_uses_json_not_protobuf(writer: FlightyClient) -> None:
    route = respx.post(API_BASE + "/v1/user/settings/viewedYearInReview").respond(
        200, json={"ok": True}
    )
    result = await writer.request("mark_year_in_review_viewed", TOKEN, payload={"year": "2000"})
    assert json.loads(route.calls[0].request.content) == {"year": "2000"}
    assert route.calls[0].request.headers["Content-Type"] == "application/json"
    assert result["data"] == {"ok": True}


async def test_get_flight_uses_sharing_response_not_bare_flight(client: FlightyClient) -> None:
    body = proto.encode("GetSharingLinkResponseProto", {"allFlights": [flight_record()]})
    respx.get(API_BASE + f"/v1/flight/{FLIGHT}").respond(
        200, content=body, headers={"Content-Type": "application/x-protobuf"}
    )
    assert (await client.request("get_flight", TOKEN, path={"flight_id": FLIGHT}))["data"][
        "allFlights"
    ][0]["id"] == FLIGHT


async def test_device_header_only_when_configured() -> None:
    client = FlightyClient(Settings(device_id="synthetic-device"))
    route = respx.get(API_BASE + "/v1/radar/layers").respond(200)
    await client.request("get_radar_layers", TOKEN)
    assert route.calls[0].request.headers["Device"] == "synthetic-device"


async def test_avatar_binary_response(client: FlightyClient) -> None:
    respx.get(API_BASE + f"/v1/user/{OWNER}/avatar").respond(
        200, content=b"fake-image", headers={"Content-Type": "image/png"}
    )
    assert (await client.request("get_avatar", TOKEN, path={"user_id": OWNER}))[
        "base64"
    ] == "ZmFrZS1pbWFnZQ=="


async def test_explicit_app_headers_and_request_timeout() -> None:
    client = FlightyClient(Settings(device_id=FLIGHT, locale="en_GB"))
    route = mock_sync()
    await client.snapshot(TOKEN)
    request = route.calls[0].request
    assert dict(request.headers) == {
        "host": "api.flightyapp.com",
        "content-length": "0",
        "connection": "keep-alive",
        "authorization": "Bearer " + TOKEN,
        "accept": "application/x-protobuf",
        "content-type": "application/x-protobuf",
        "accept-encoding": "gzip",
        "user-agent": "Flighty 4.12.0 (4875) com.flightyapp.flighty",
        "x-flighty-locale": "en_GB",
        "x-flighty-client-capabilities": capability_header(),
        "device": FLIGHT,
    }
    assert request.extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 60)


async def test_read_refresh_interval_applies_even_when_cache_ttl_zero() -> None:
    client = FlightyClient(Settings(cache_ttl=0))
    route = mock_sync()
    await client.snapshot(TOKEN)
    await asyncio.gather(*(client.snapshot(TOKEN, refresh=True) for _ in range(5)))
    assert route.call_count == 1
    client.invalidate(TOKEN)
    await client.snapshot(TOKEN, refresh=True)
    assert route.call_count == 2


async def test_fresh_read_before_write_does_not_reuse_throttled_snapshot(
    writer: FlightyClient,
) -> None:
    route = mock_sync()
    await writer.snapshot(TOKEN)
    await writer.snapshot(TOKEN, refresh=True, fresh_for_write=True)
    assert route.call_count == 2


async def test_fresh_for_write_requires_write_permission(client: FlightyClient) -> None:
    with pytest.raises(WritesDisabled):
        await client.snapshot(TOKEN, fresh_for_write=True)
    assert not respx.calls


async def test_protocol_search_rate_limit_is_not_an_empty_success(client: FlightyClient) -> None:
    respx.post(API_BASE + "/v1/search").respond(
        200,
        content=proto.encode("SearchResponseProto", {"error": "TOO_MANY_REQUESTS"}),
        headers={"Content-Type": "application/x-protobuf"},
    )
    with pytest.raises(FlightyError, match="search rate limit"):
        await client.request(
            "search_flights",
            TOKEN,
            payload={
                "departureDate": "2099-01-02",
                "flightNumberSearch": {"airlineId": FLIGHT, "flightNumber": "123"},
            },
        )
    assert len(respx.calls) == 1


async def test_content_sync_uses_default_priority_and_conditional_etag(
    client: FlightyClient,
) -> None:
    route = respx.post(API_BASE + f"/v1/sync/content/flight/{FLIGHT}").respond(304)
    result = await client.request(
        "poll_flight",
        TOKEN,
        path={"flight_id": FLIGHT},
        headers={"If-None-Match": '"synthetic-etag"'},
    )
    assert result["not_modified"]
    assert route.calls[0].request.headers["X-Sync-Priority"] == "0"
    assert route.calls[0].request.headers["If-None-Match"] == '"synthetic-etag"'
    assert route.calls[0].request.content == b""
