"""Flighty 4.12 regression cases: independent wire bytes and intercepted HTTP only."""

from __future__ import annotations

import base64
import copy

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from flighty_mcp import proto
from flighty_mcp.capabilities import capability_header, client_capabilities
from flighty_mcp.client import FlightyClient
from flighty_mcp.config import API_BASE
from flighty_mcp.errors import FlightyError
from flighty_mcp.server import create_server
from flighty_mcp.service import FlightyService

from .conftest import FLIGHT, OTHER, OWNER, SYNC, TOKEN

CAPABILITY_WIRE = (
    b"\x0a\x0b\x0a\x07AIRPORT\x10\x01\x0a\x16\x0a\x12WHEN_TO_LEAVE_PLAN\x10\x01\x12\x03\x01\x02\x03"
)


def test_capabilities_match_independent_map_and_packed_enum_wire() -> None:
    assert proto.encode("ClientCapabilitiesProto", client_capabilities()) == CAPABILITY_WIRE
    header = capability_header()
    assert not any(c in header for c in "+/=")
    assert base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)) == CAPABILITY_WIRE
    assert proto.decode("ClientCapabilitiesProto", CAPABILITY_WIRE) == client_capabilities()


@pytest.mark.parametrize(
    "payload,wire",
    [
        ({"profileRemoveAvatarAction": {}}, bytes.fromhex("ea0200")),
        (
            {"whenToLeavePlanChangeAction": {"planId": "p", "operation": "DELETE"}},
            bytes.fromhex("f202050a01701803"),
        ),
        (
            {"whenToLeavePlanChangeAction": {"planId": "p", "operation": "REQUEST_REFRESH"}},
            bytes.fromhex("f202050a01701805"),
        ),
        ({"passportSharedAction": {"year": 2026}}, bytes.fromhex("fa020308ea0f")),
    ],
)
def test_new_actions_and_enum_gaps_match_independent_wire(payload: dict, wire: bytes) -> None:
    assert proto.encode("SyncChangeRequestProto", payload) == wire
    assert proto.decode("SyncChangeRequestProto", wire) == payload


def test_optional_planner_zero_and_int64_varints() -> None:
    payload = {"travelMode": "MANUAL", "manualDurationSeconds": "1200", "travelBufferSeconds": "0"}
    wire = bytes.fromhex("080220b0093000")
    assert proto.encode("RemoteWhenToLeavePlan.Inputs", payload) == wire
    assert proto.decode("RemoteWhenToLeavePlan.Inputs", wire) == payload
    # seconds has presence even when zero; PARKING is 9, rather than declaration order 1.
    assert proto.encode(
        "RemoteWhenToLeavePlan.Allowance.Item", {"activity": "PARKING", "seconds": "0"}
    ) == bytes.fromhex("08091000")
    assert proto.decode("RemoteWhenToLeavePlan", bytes.fromhex("3803")) == {"lifecycle": "STOPPED"}


def test_new_airport_asset_and_distribution_wire() -> None:
    assert proto.decode("RemoteAirport", bytes.fromhex("880101")) == {"isSchengen": True}
    assert proto.decode("StaticAssetProto", bytes.fromhex("0807")) == {
        "kind": "USER_STATS_DISTRIBUTIONS"
    }
    assert proto.decode(
        "CLIUserStatsDistributionsProto.Threshold", bytes.fromhex("080510ac02")
    ) == {"topPercent": 5, "value": "300"}


async def test_planner_lookups_work_with_writes_disabled(client: FlightyClient) -> None:
    holidays = respx.get(API_BASE + f"/v1/when-to-leave/online/{FLIGHT}/holidays").respond(
        200, content=bytes.fromhex("0a025553"), headers={"Content-Type": "application/x-protobuf"}
    )
    estimate = respx.post(API_BASE + f"/v1/when-to-leave/online/{FLIGHT}/estimate").respond(
        200, content=bytes.fromhex("48d005"), headers={"Content-Type": "application/x-protobuf"}
    )
    server = create_server(client)
    result = await server.call_tool("get_when_to_leave_holidays", {"path": {"flight_id": FLIGHT}})
    assert result.structured_content["data"] == {"countryCode": "US"}
    assert holidays.calls[0].request.content == b""
    assert not holidays.calls[0].request.url.query
    result = await server.call_tool(
        "estimate_when_to_leave",
        {
            "path": {"flight_id": FLIGHT},
            "payload": {
                "travelMode": "MANUAL",
                "manualDurationSeconds": "1200",
                "travelBufferSeconds": "0",
            },
        },
    )
    request = estimate.calls[0].request
    assert request.content == bytes.fromhex("080220b0093000")
    assert request.headers["Content-Type"] == "application/x-protobuf"
    assert request.headers["X-Flighty-Client-Capabilities"] == capability_header()
    assert not request.url.query
    assert result.structured_content["data"] == {"durationSeconds": "720"}
    assert result.structured_content["meta"]["schema_version"] == "4.12.0"


async def test_plan_identity_updates_tombstones_ownership_and_pagination(
    client: FlightyClient,
) -> None:
    def plan(plan_id: str, **changes: object) -> dict:
        return {
            "planId": plan_id,
            "userId": OWNER,
            "flight": {"kind": "ONLINE", "flightId": FLIGHT},
            "updated": "2000-01-02T00:00:00Z",
            "lifecycle": "ACTIVE",
            **changes,
        }

    newer = plan("p", lifecycle="STOPPED", updated="2000-01-03T00:00:00Z")
    pages = [
        {
            "data": [
                {"remoteWhenToLeavePlan": item}
                for item in [
                    plan("p"),
                    plan("q"),
                    plan("same-flight-different-plan"),
                    plan("p", userId=OTHER),
                ]
            ],
            "pagination": {"moreData": True, "nextUrl": "?cursor=two"},
        },
        {
            "data": [
                {"remoteWhenToLeavePlan": item}
                for item in [
                    newer,
                    plan("p"),
                    plan("q", lifecycle="DELETED", updated="2000-01-04T00:00:00Z"),
                    plan("timestamp-deleted", deleted="2000-01-05T00:00:00Z"),
                ]
            ]
        },
    ]
    second = respx.post(SYNC, params={"cursor": "two"}).respond(
        200,
        content=proto.encode("SyncResponseProto", pages[1]),
        headers={"Content-Type": "application/x-protobuf"},
    )
    first = respx.post(SYNC).respond(
        200,
        content=proto.encode("SyncResponseProto", pages[0]),
        headers={"Content-Type": "application/x-protobuf"},
    )
    service = FlightyService(client)
    result = await service.list_when_to_leave_plans(TOKEN, flight_id=FLIGHT, limit=1)
    assert result["total"] == 2
    assert result["plans"] == [newer]
    assert result["next_offset"] == 1
    records = await service.account_records(TOKEN, "remoteWhenToLeavePlan")
    assert len(records["records"]) == 2
    assert all(item["userId"] == OWNER for item in records["records"])
    snapshot = await client.snapshot(TOKEN)
    assert len(snapshot.owned("remoteWhenToLeavePlan", include_deleted=True)) == 4
    assert first.call_count == second.call_count == 1
    assert all(call.request.content == b"" for call in respx.calls)


@pytest.mark.parametrize("operation", ["DELETE", 3])
async def test_nested_plan_delete_cannot_bypass_confirmation(
    writer: FlightyClient, operation: object
) -> None:
    changes = [
        {"flightSetMineAction": {"flightId": FLIGHT}},
        {"whenToLeavePlanChangeAction": {"planId": "p", "operation": operation}},
    ]
    with pytest.raises(FlightyError, match="confirm_destructive"):
        await writer.apply_changes(TOKEN, changes)
    assert not respx.calls


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "DELETE"},
        {"planId": "p", "operation": "UNKNOWN"},
        {"planId": "p", "operation": 4},
        {"planId": "p", "operation": True},
        {"planId": "p", "operation": "SAVE", "inputs": {"travelMode": "MANUAL"}},
        {
            "planId": "p",
            "operation": "SAVE",
            "flight": {"kind": "ONLINE", "flightId": FLIGHT},
            "inputs": {},
        },
        {"planId": "p", "operation": "DISABLE", "flight": {"kind": [], "flightId": FLIGHT}},
        {"planId": "p", "operation": "DISABLE", "flight": {"kind": "ONLINE", "flightId": None}},
        {"planId": "p", "operation": "SIMULATE_TRAFFIC", "simulatedTrafficSeconds": -1},
        {"planId": "p", "operation": "SIMULATE_TRAFFIC", "simulatedTrafficSeconds": True},
        {"planId": "p", "operation": "DISABLE", "simulatedTrafficSeconds": 60},
    ],
)
async def test_invalid_plan_batch_never_reaches_http(writer: FlightyClient, body: dict) -> None:
    with pytest.raises(FlightyError):
        await writer.apply_changes(
            TOKEN, [{"whenToLeavePlanChangeAction": body}], confirm_destructive=True
        )
    assert not respx.calls


async def test_plan_save_does_not_mutate_inputs_and_needs_readback(writer: FlightyClient) -> None:
    route = respx.post(SYNC).respond(200)
    inputs = {
        "travelMode": "MANUAL",
        "manualDurationSeconds": "1200",
        "departureRemindersRequested": True,
    }
    original = copy.deepcopy(inputs)
    result = await create_server(writer).call_tool(
        "save_when_to_leave_plan", {"plan_id": "p", "flight_id": FLIGHT, "inputs": inputs}
    )
    body = proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"][0]
    assert body["whenToLeavePlanChangeAction"] == {
        "planId": "p",
        "flight": {"kind": "ONLINE", "flightId": FLIGHT},
        "operation": "SAVE",
        "inputs": original,
    }
    assert inputs == original
    assert result.structured_content["readback_required"] is True
    assert route.call_count == 1


@pytest.mark.parametrize(
    "operation", ["DISABLE", "DELETE", "REQUEST_REFRESH", "SIMULATE_TRAFFIC", "RESET_TRAFFIC_ALERT"]
)
async def test_plan_commands_send_one_sync_change(writer: FlightyClient, operation: str) -> None:
    route = respx.post(SYNC).respond(200)
    args = {"plan_id": "p", "operation": operation, "confirm_destructive": operation == "DELETE"}
    if operation == "SIMULATE_TRAFFIC":
        args["simulated_traffic_seconds"] = 600
    await create_server(writer).call_tool("change_when_to_leave_plan", args)
    changes = proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"]
    assert len(changes) == 1
    assert changes[0]["whenToLeavePlanChangeAction"]["operation"] == operation
    assert route.call_count == 1


async def test_avatar_removal_requires_confirmation_at_mcp_boundary(writer: FlightyClient) -> None:
    server = create_server(writer)
    with pytest.raises(ToolError, match="confirm_destructive"):
        await server.call_tool("remove_profile_avatar", {})
    assert not respx.calls
    route = respx.post(SYNC).respond(200)
    await server.call_tool("remove_profile_avatar", {"confirm_destructive": True})
    assert (
        proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"][0][
            "profileRemoveAvatarAction"
        ]
        == {}
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        (
            "save_when_to_leave_plan",
            {"plan_id": "p", "flight_id": FLIGHT, "inputs": {"travelMode": "MANUAL"}},
        ),
        (
            "change_when_to_leave_plan",
            {"plan_id": "p", "operation": "DELETE", "confirm_destructive": True},
        ),
        ("remove_profile_avatar", {"confirm_destructive": True}),
        ("record_passport_shared", {"year": 2026}),
    ],
)
async def test_new_write_tools_respect_read_only_mode(
    client: FlightyClient, tool: str, args: dict
) -> None:
    with pytest.raises(ToolError, match="disabled"):
        await create_server(client).call_tool(tool, args)
    assert not respx.calls


@pytest.mark.parametrize("year", [0, -1, 10000, True])
async def test_invalid_share_year_rejected_before_request(
    writer: FlightyClient, year: object
) -> None:
    with pytest.raises(FlightyError, match="year"):
        await writer.apply_changes(TOKEN, [{"passportSharedAction": {"year": year}}])
    assert not respx.calls


async def test_passport_share_is_explicit_event_only(writer: FlightyClient) -> None:
    route = respx.post(SYNC).respond(200)
    await create_server(writer).call_tool(
        "record_passport_shared", {"year": 2026, "occurred_at": "2026-12-08T00:00:00Z"}
    )
    assert proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"][0][
        "passportSharedAction"
    ] == {"year": 2026, "occurredAt": "2026-12-08T00:00:00Z"}
    assert route.call_count == 1


async def test_device_registration_and_sync_share_capabilities(writer: FlightyClient) -> None:
    registration = respx.post(API_BASE + "/v1/device").respond(200)
    sync = respx.post(SYNC).respond(200)
    payload = {"id": "synthetic-device"}
    await writer.request("register_device", TOKEN, payload=payload)
    await writer.apply_changes(TOKEN, [{"deviceChangeAction": payload}])
    assert payload == {"id": "synthetic-device"}
    for request, typename in [
        (registration.calls[0].request, "CreateDeviceRequestProto"),
        (sync.calls[0].request, "SyncRequestProto"),
    ]:
        body = proto.decode(typename, request.content)
        if typename == "SyncRequestProto":
            body = body["changes"][0]["deviceChangeAction"]
        assert body["capabilities"] == client_capabilities()
        assert request.headers["X-Flighty-Client-Capabilities"] == capability_header()
