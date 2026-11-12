"""Every generic endpoint wrapper is exercised with fake credentials and mocked HTTP."""

from __future__ import annotations

import json
import re

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from flighty_mcp import proto
from flighty_mcp.capabilities import client_capabilities
from flighty_mcp.client import FlightyClient
from flighty_mcp.config import API_BASE
from flighty_mcp.endpoints import ENDPOINTS
from flighty_mcp.server import create_server

from .conftest import AIRPORT_A, FLIGHT, OWNER

SPECIAL = {
    "search_flights",
    "get_flight",
    "poll_flight",
    "subscribe_flight",
    "subscribe_random_flight",
    "sync_account",
    "apply_changes",
    "delete_account",
}


@pytest.mark.parametrize("operation", sorted(set(ENDPOINTS) - SPECIAL))
async def test_named_generic_endpoint_route_method_headers_and_body_mock_only(
    writer: FlightyClient, operation: str
) -> None:
    endpoint = ENDPOINTS[operation]
    parameters = {
        "airport_id": AIRPORT_A,
        "flight_id": FLIGHT,
        "user_id": OWNER,
        "share_id": "synthetic-share",
        "invite_id": "synthetic-invite",
        "code": "SYNTHETIC-CODE",
        "direction": "DEPARTURE",
    }
    path = {key: parameters[key] for key in re.findall(r"{(\w+)}", endpoint.path)}
    payload = {} if endpoint.request else None
    if endpoint.json_body:
        payload = {
            "submit_attribution": {"token": "synthetic-attribution", "service": "ASA"},
            "mark_year_in_review_viewed": {"year": "2099"},
            "create_share_link": {"items": ["https://live.flighty.app/synthetic-share"]},
        }[operation]
    headers = (
        {"Content-Type": "image/png"}
        if endpoint.binary_response
        else {"Content-Type": "application/x-protobuf"}
    )
    content = b"synthetic-image" if endpoint.binary_response else b""
    if endpoint.json_body:
        headers = {"Content-Type": "application/json"}
        content = b'{"ok":true}'
    route = respx.request(endpoint.method, API_BASE + endpoint.path.format(**path)).respond(
        200, content=content, headers=headers
    )
    args = {"payload": payload, "path": path, "confirm_destructive": endpoint.destructive}
    result = await create_server(writer).call_tool(operation, args)
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.method == endpoint.method
    if endpoint.request:
        expected = (
            {"capabilities": client_capabilities(), **payload}
            if operation == "register_device"
            else payload
        )
        assert request.content == proto.encode(endpoint.request, expected)
    elif endpoint.json_body:
        assert json.loads(request.content) == payload
    else:
        assert request.content == b""
    assert result.structured_content["meta"]["operation"] == operation


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("submit_attribution", {"token": "synthetic", "service": "wrong"}),
        ("mark_year_in_review_viewed", {"year": 2099}),
        ("create_share_link", {"items": ["https://untrusted.invalid/secret"]}),
        ("create_share_link", {"items": []}),
    ],
)
async def test_json_validation_precedes_any_upstream_request(
    writer: FlightyClient, operation: str, payload: dict
) -> None:
    with pytest.raises(ToolError):
        await create_server(writer).call_tool(operation, {"payload": payload})
    assert not respx.calls


@pytest.mark.parametrize("operation", ["link_account", "import_calendar"])
async def test_link_and_calendar_require_confirmation(
    writer: FlightyClient, operation: str
) -> None:
    with pytest.raises(ToolError, match="confirm_destructive"):
        await create_server(writer).call_tool(operation, {"payload": {}})
    assert not respx.calls


async def test_calendar_source_is_locally_validated(writer: FlightyClient) -> None:
    with pytest.raises(ToolError, match="source"):
        await create_server(writer).call_tool(
            "import_calendar",
            {"payload": {}, "query": {"source": "unrecognized"}, "confirm_destructive": True},
        )
    assert not respx.calls


@pytest.mark.parametrize("direction", ["DEPARTURE", "ARRIVAL"])
async def test_airport_board_direction_literals(writer: FlightyClient, direction: str) -> None:
    route = respx.get(API_BASE + f"/v1/airport-board/{AIRPORT_A}/{direction}").respond(200)
    await create_server(writer).call_tool(
        "get_airport_board", {"path": {"airport_id": AIRPORT_A, "direction": direction}}
    )
    assert route.called
