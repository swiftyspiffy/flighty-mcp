from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from flighty_mcp import proto
from flighty_mcp.client import FlightyClient
from flighty_mcp.config import API_BASE, Settings
from flighty_mcp.endpoints import ENDPOINTS
from flighty_mcp.errors import FlightyError
from flighty_mcp.server import create_server, resolve_token

from .conftest import AIRLINE, FLIGHT, OTHER_TOKEN, OWNER, SYNC, TOKEN, mock_sync


def test_http_auth_precedence_and_no_environment_fallback() -> None:
    assert (
        resolve_token(
            SimpleNamespace(
                headers={"X-Flighty-Token": OTHER_TOKEN, "Authorization": "Bearer " + TOKEN}
            )
        )
        == OTHER_TOKEN
    )
    assert (
        resolve_token(SimpleNamespace(headers={"Authorization": "bEaReR " + OTHER_TOKEN}))
        == OTHER_TOKEN
    )
    for headers in ({}, {"authorization": "Basic invalid"}):
        with pytest.raises(FlightyError, match="HTTP requests need"):
            resolve_token(SimpleNamespace(headers=headers))
    assert resolve_token(None) == TOKEN


async def test_discovery_exposes_all_routes_without_network(client: FlightyClient) -> None:
    server = create_server(client)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert set(ENDPOINTS) <= names
    assert len(names) == 55
    for tool in tools:
        assert "ctx" not in tool.input_schema.get("properties", {})
        if tool.name in ENDPOINTS:
            assert tool.annotations.read_only_hint == ENDPOINTS[tool.name].read_only
    result = await server.call_tool("get_endpoint_catalog", {})
    assert result.structured_content["operations"]["sync_account"]["read_only"] is True
    assert not respx.calls


async def test_mcp_listing_and_resource_use_same_cache(client: FlightyClient) -> None:
    route = mock_sync()
    server = create_server(client)
    result = await server.call_tool("list_flights", {"when": "upcoming"})
    assert result.structured_content["total"] == 1
    resource = list(await server.read_resource("flighty://flights/upcoming"))
    assert json.loads(resource[0].content)["total"] == 1
    assert route.call_count == 1
    assert TOKEN not in resource[0].content


async def test_search_flights_constructs_oneof_and_wire_date(client: FlightyClient) -> None:
    route = respx.post(API_BASE + "/v1/search").respond(
        200,
        content=proto.encode("SearchResponseProto", {"response": {}}),
        headers={"Content-Type": "application/x-protobuf"},
    )
    await create_server(client).call_tool(
        "search_flights",
        {"departure_date": "2099-01-02", "airline_id": AIRLINE, "flight_number": "123"},
    )
    body = proto.decode("SearchRequestProto", route.calls[0].request.content)
    assert body == {
        "flightNumberSearch": {"airlineId": AIRLINE, "flightNumber": "123"},
        "departureDate": "2099-01-02",
    }


async def test_mcp_write_rejected_before_http_when_disabled(client: FlightyClient) -> None:
    with pytest.raises(ToolError, match="disabled"):
        await create_server(client).call_tool(
            "set_flight_note", {"flight_id": FLIGHT, "note": "synthetic"}
        )
    assert not respx.calls


async def test_default_server_allows_note_write_but_blocks_account_deletion() -> None:
    route = respx.post(SYNC).respond(200)
    server = create_server()
    await server.call_tool("set_flight_note", {"flight_id": FLIGHT, "note": "synthetic"})
    assert route.call_count == 1
    changes = proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"]
    assert changes[0]["userFlightNoteUploadChangeAction"] == {
        "flightId": FLIGHT,
        "note": "synthetic",
    }
    with pytest.raises(ToolError, match="disabled"):
        await server.call_tool("delete_account", {"confirmation_user_id": OWNER})
    assert route.call_count == 1


async def test_note_write_keeps_http_reply_small_with_large_sync_response(
    writer: FlightyClient,
) -> None:
    marker = "synthetic-large-returned-note-"
    body = proto.encode(
        "SyncResponseProto",
        {
            "data": [
                {
                    "remoteUserFlightNote": {
                        "flightId": FLIGHT,
                        "note": marker + "x" * (11 * 1024 * 1024),
                    }
                }
            ],
            "pagination": {"moreData": True, "nextUrl": "synthetic-private-cursor"},
        },
    )
    assert len(body) > 10 * 1024 * 1024
    route = respx.post(SYNC).respond(
        200, content=body, headers={"Content-Type": "application/x-protobuf"}
    )
    app = create_server(writer).streamable_http_app(stateless_http=True, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http,
    ):
        response = await http.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "X-Flighty-Token": TOKEN},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_flight_note",
                    "arguments": {"flight_id": FLIGHT, "note": "synthetic-new-note"},
                },
            },
        )
    assert response.status_code == 200
    assert len(response.content) < 4096
    result = response.json()["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["submitted_changes"] == 1
    assert result["structuredContent"]["readback_required"] is True
    assert result["structuredContent"]["data"] == {
        "returned_record_counts": {"remoteUserFlightNote": 1},
        "more_data": True,
    }
    assert marker not in response.text
    assert "synthetic-private-cursor" not in response.text
    assert route.call_count == 1


async def test_delete_confirmation_required_before_http(writer: FlightyClient) -> None:
    with pytest.raises(ToolError, match="confirmation"):
        await create_server(writer).call_tool(
            "delete_flight", {"flight_id": FLIGHT, "confirmation": "wrong"}
        )
    assert not respx.calls


@pytest.mark.parametrize(
    "manual,action_name,wire_prefix",
    [
        (False, "flightDeleteAction", b"\x5a\x26\x0a\x24"),
        (True, "deleteManualFlightAction", b"\x92\x02\x26\x0a\x24"),
    ],
)
async def test_own_flight_deletes_omit_owner_and_match_wire_fixture(
    writer: FlightyClient, manual: bool, action_name: str, wire_prefix: bytes
) -> None:
    route = respx.post(SYNC).respond(200)
    await create_server(writer).call_tool(
        "delete_flight", {"flight_id": FLIGHT, "confirmation": FLIGHT, "manual": manual}
    )
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer " + TOKEN
    changes = proto.decode("SyncRequestProto", request.content)["changes"]
    assert len(changes) == 1
    assert changes[0][action_name] == {"flightId": FLIGHT}
    # Independent field tags/lengths: action 11 or 34, then only UUID field 1.
    # ownerUserId must be absent on the wire, not an empty or explicit self ID.
    assert request.content.endswith(wire_prefix + FLIGHT.encode("ascii"))


async def test_account_delete_requires_both_switches_and_identity() -> None:
    client = FlightyClient(Settings(allow_writes=True, allow_account_deletion=True))
    server = create_server(client)
    with pytest.raises(ToolError, match="confirmation"):
        await server.call_tool("delete_account", {"confirmation_user_id": "wrong"})
    route = respx.delete(API_BASE + "/v1/user").respond(204)
    await server.call_tool("delete_account", {"confirmation_user_id": OWNER})
    assert route.call_count == 1
    assert route.calls[0].request.content == b""


@pytest.mark.parametrize(
    "tool,args",
    [
        ("set_flight_archived", {"flight_id": FLIGHT, "archived": True}),
        ("set_flight_archived", {"flight_id": FLIGHT, "archived": False}),
        ("set_flight_mine", {"flight_id": FLIGHT, "is_mine": True}),
        ("set_flight_mine", {"flight_id": FLIGHT, "is_mine": False}),
        ("set_flight_note", {"flight_id": FLIGHT, "note": ""}),
    ],
)
async def test_common_edits_generate_one_action_mock_only(
    writer: FlightyClient, tool: str, args: dict
) -> None:
    route = respx.post(SYNC).respond(200)
    await create_server(writer).call_tool(tool, args)
    changes = proto.decode("SyncRequestProto", route.calls[0].request.content)["changes"]
    assert len(changes) == 1
    assert len(changes[0]) == 2
    assert "timestamp" in changes[0]


async def test_read_only_http_handshake_and_no_credential_fallback(client: FlightyClient) -> None:
    server = create_server(client)
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http,
    ):
        common = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialized = await http.post(
            "/mcp",
            headers=common,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "flighty"
        no_auth = await http.post(
            "/mcp",
            headers=common,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_flights", "arguments": {}},
            },
        )
        assert no_auth.json()["result"]["isError"] is True
        assert not respx.calls
        route = mock_sync()
        good = await http.post(
            "/mcp",
            headers={**common, "X-Flighty-Token": OTHER_TOKEN},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "sync_account", "arguments": {}},
            },
        )
        assert good.json()["result"].get("isError", False) is False
        assert route.calls[0].request.headers["Authorization"] == "Bearer " + OTHER_TOKEN


async def test_websocket_subscription_is_binary_and_redacts_credential(
    client: FlightyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Socket:
        sent = []

        async def send(self, data: bytes) -> None:
            self.sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return proto.encode("FlightyWebsocketsServerMessage", {"requestSync": {}})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    socket = Socket()
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *a, **kw: socket)
    result = await create_server(client).call_tool("wait_for_sync", {"timeout_seconds": 1})
    assert result.structured_content == {"sync_requested": True}
    assert proto.decode("FlightyWebsocketsClientMessage", socket.sent[0]) == {
        "registerSession": {"tokens": [TOKEN]}
    }
    assert TOKEN not in str(result)
