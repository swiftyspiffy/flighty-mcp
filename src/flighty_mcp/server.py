"""MCP tools/resources over stdio or per-request-authenticated streamable HTTP."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

from . import __version__, proto
from .client import FlightyClient, token_subject, utc_now, uuid_text, validate_token
from .config import WEBSOCKET_URL, secret_env
from .endpoints import ENDPOINTS, catalog
from .errors import FlightyError
from .service import FlightyService, cache_info


def resolve_token(ctx: Context | None) -> str:
    """HTTP never falls back to a server-wide environment credential."""
    try:
        headers = ctx.headers if ctx is not None else None
    except ValueError:
        # Direct in-process SDK calls have no transport request context.
        headers = None
    if headers is not None:
        lowered = {name.lower(): value for name, value in headers.items()}
        token = lowered.get("x-flighty-token", "").strip()
        if token:
            return validate_token(token)
        auth = lowered.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return validate_token(auth[7:])
        raise FlightyError(
            "HTTP requests need X-Flighty-Token or Authorization: Bearer. Environment credentials are not used over HTTP."
        )
    return validate_token(secret_env("FLIGHTY_TOKEN"))


READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
LOCAL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def create_server(client: FlightyClient | None = None) -> MCPServer:
    api = client or FlightyClient()
    service = FlightyService(api)
    mcp = MCPServer(
        "flighty",
        version=__version__,
        debug=False,
        log_level="WARNING",
        instructions=(
            "Unofficial Flighty integration. Read tools use the app's bodyless sync download path. "
            "Use list_flights, search_airports and search_airlines to discover real IDs. "
            "Use get_schema before constructing advanced protobuf payloads; JSON is encoded to binary protobuf. "
            "Writes are enabled by default and require explicit user intent. Account deletion is disabled by default. "
            "Core flight edits have been live-tested; additional account and integration operations have only mock coverage. "
            "Do not delete, import, link accounts, register devices or submit bulk changes without a specific request. "
            "Never ask for bearer tokens in tool arguments; credentials arrive through env or request headers. "
            "Omitted optional mutation fields may clear existing values; use update_ticket to preserve known fields. "
            "Rate limits are undocumented. Use the account cache and avoid polling unnecessarily."
        ),
    )

    @mcp.tool(annotations=LOCAL)
    def get_schema(name: str = "") -> dict[str, Any]:
        """Inspect supported protobuf fields, numbers, types, enum values and oneof groups.

        Empty/partial name lists matching definitions. Full names, for example
        SyncChangeRequestProto.TicketInfoAction, show the complete field contract.
        Advanced tool payloads use ProtoJSON: RFC3339 timestamps, enum names,
        base64 bytes, and exactly one populated member per oneof group.
        This tool performs no network requests.
        """
        return proto.describe(name)

    @mcp.tool(annotations=LOCAL)
    def get_endpoint_catalog() -> dict[str, Any]:
        """List every supported HTTP operation, request/response schema and read/write classification.

        Includes the app-only TripIt browser flow and websocket protocol metadata.
        Core flight edits have been live-tested; see docs/testing.md for coverage.
        """
        return catalog()

    @mcp.tool(annotations=READ)
    async def list_flights(
        when: Literal["all", "upcoming", "past"] = "all",
        mine_only: bool = False,
        archived: bool | None = None,
        origin: str | None = None,
        destination: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
        details: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List this account's current flights, including archived history and manual flights.

        Excludes server deletion markers and other owners' flight records. mine_only
        keeps flights marked My Flights; false also includes flights this account tracks.
        Date filters are YYYY-MM-DD in the departure airport's local timezone. Airport
        filters accept IATA, name or ID text. Sorted by departure time, earliest first.
        Pagination is local after a complete, cached, bodyless account sync. limit 1..200.
        details=true also includes the full decoded user-flight record.
        """
        return await service.list_flights(
            resolve_token(ctx),
            when=when,
            mine_only=mine_only,
            archived=archived,
            origin=origin,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
            refresh=refresh,
            details=details,
        )

    @mcp.tool(annotations=READ)
    async def sync_account(refresh: bool = True, ctx: Context | None = None) -> dict[str, Any]:
        """Download all account pages without submitting changes, returning counts and cache metadata.

        Uses POST /v1/sync/full with NO body on every page. Not the apply_changes tool.
        Repeated read refreshes within 60 seconds reuse the last complete snapshot.
        get_account_records exposes individual record categories after the download.
        """
        snapshot = await api.snapshot(resolve_token(ctx), refresh=refresh)
        return {
            "account_id": snapshot.account_id,
            "counts": {kind: len(items) for kind, items in snapshot.records.items()},
            "cache": cache_info(snapshot),
            "writes_enabled": api.settings.allow_writes,
            "account_deletion_enabled": api.settings.allow_account_deletion,
        }

    @mcp.tool(annotations=READ)
    async def get_account_records(
        kind: str,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Read a sync category: tickets, notes, subscriptions, profile, friends, favorites, settings, etc.

        Discover category names with get_schema('SyncResponseData'). Excludes deletion
        markers; flight categories are restricted to this account. Token/secret/receipt
        fields are redacted. limit 1..200. No outgoing account changes are sent.
        """
        return await service.account_records(
            resolve_token(ctx), kind, limit=limit, offset=offset, refresh=refresh
        )

    def register_catalog_tool(name: str, kind: str, description: str) -> None:
        async def search(query: str, limit: int = 20, ctx: Context | None = None) -> dict[str, Any]:
            return await service.search_catalog(resolve_token(ctx), kind, query, limit)

        mcp.add_tool(search, name=name, description=description, annotations=READ)

    register_catalog_tool(
        "search_airports",
        "remoteAirport",
        "Search airports by IATA/ICAO code, name, city or country. Returns IDs needed by route searches, airport status and manual-flight edits. Uses cached reference data from a read-only sync. limit 1..100.",
    )
    register_catalog_tool(
        "search_airlines",
        "remoteAirline",
        "Search airlines by IATA/ICAO code or name. Returns the airlineId required for flight-number searches. Uses cached read-only account reference data. limit 1..100.",
    )
    register_catalog_tool(
        "search_aircraft_types",
        "remoteAircraftType",
        "Search aircraft type reference data by IATA/ICAO code or model name. Returns equipment IDs for manual flights. limit 1..100.",
    )
    register_catalog_tool(
        "search_metro_areas",
        "remoteMetroArea",
        "Search metro-area reference data for multi-airport route searches. limit 1..100.",
    )

    @mcp.tool(annotations=READ)
    async def search_flights(
        departure_date: str,
        airline_id: str | None = None,
        flight_number: str | None = None,
        origin_id: str | None = None,
        destination_id: str | None = None,
        origin_kind: Literal["airport", "metro"] = "airport",
        destination_kind: Literal["airport", "metro"] = "airport",
        via: str = "",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search available flights by airline+number OR by origin+destination on YYYY-MM-DD.

        Search alone does not add/subscribe to a flight. Discover IDs using the
        airport/airline/metro lookup tools first. Pass a numeric flight_number without
        airline prefix. Route search endpoints can be airport or metro IDs.
        """
        try:
            datetime.strptime(departure_date, "%Y-%m-%d")
        except ValueError:
            raise FlightyError("departure_date must use YYYY-MM-DD.") from None
        body: dict[str, Any] = {"departureDate": departure_date, "via": via}
        if airline_id and flight_number and not origin_id and not destination_id:
            body["flightNumberSearch"] = {
                "airlineId": uuid_text(airline_id),
                "flightNumber": flight_number,
            }
        elif origin_id and destination_id and not airline_id and not flight_number:
            body["routeSearchProto"] = {
                "origin": {origin_kind: {"id": uuid_text(origin_id)}},
                "destination": {destination_kind: {"id": uuid_text(destination_id)}},
            }
        else:
            raise FlightyError(
                "Provide airline_id+flight_number OR origin_id+destination_id, not both."
            )
        return await api.request("search_flights", resolve_token(ctx), payload=body)

    @mcp.tool(annotations=READ)
    async def get_flight(flight_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Fetch a flight by UUID using the app's GET /v1/flight/{id} sharing/details response.

        Does not add the flight. Returns allFlights, traveler profiles, manualFlights
        and airport-status data when supplied by the server; account tokens are redacted.
        """
        return await api.request("get_flight", resolve_token(ctx), path={"flight_id": flight_id})

    @mcp.tool(annotations=READ)
    async def poll_flight(
        flight_id: str,
        etag: str | None = None,
        priority: Literal[0, 10] = 0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Poll changing flight content, optionally using If-None-Match. Does not subscribe/add a flight.

        Uses the app's bodyless content-sync POST and X-Sync-Priority 0 or 10.
        A 304 result has not_modified=true. Prefer cached list_flights for routine reads.
        """
        headers = {"X-Sync-Priority": str(priority)}
        if etag:
            if any(c in etag for c in "\r\n\0") or len(etag) > 1024:
                raise FlightyError("Invalid ETag.")
            headers["If-None-Match"] = etag
        return await api.request(
            "poll_flight", resolve_token(ctx), path={"flight_id": flight_id}, headers=headers
        )

    @mcp.tool(annotations=WRITE)
    async def subscribe_flight(
        flight_id: str, is_passenger: bool = True, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Add/track a known flight UUID, normally discovered by search_flights.

        Mutating, requires writes enabled. is_passenger=true marks it as your flight;
        false tracks someone else's flight. Sends the app's is_passenger query string.
        """
        return await api.request(
            "subscribe_flight",
            resolve_token(ctx),
            path={"flight_id": flight_id},
            params={"is_passenger": str(is_passenger).lower()},
        )

    @mcp.tool(annotations=WRITE)
    async def subscribe_random_flight(
        is_passenger: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Add a random flight to the account. This changes tracked flights; writes must be enabled."""
        return await api.request(
            "subscribe_random_flight",
            resolve_token(ctx),
            params={"is_passenger": str(is_passenger).lower()},
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def apply_changes(
        changes: list[dict[str, Any]], confirm_destructive: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Advanced sync editor supporting all 46 supported actions, with strict protobuf validation.

        Each object has exactly one action, plus an optional RFC3339 timestamp (generated
        if omitted). Inspect get_schema('SyncChangeRequestProto') and the action's schema
        first. Requires writes enabled. Delete/remove/revoke/bulk actions additionally
        require confirm_destructive=true, including a nested plan DELETE operation.
        No automatic retries or implicit empty changes.
        Omitted fields may clear values; provide a complete action when semantics are unclear.
        """
        return await api.apply_changes(
            resolve_token(ctx), changes, confirm_destructive=confirm_destructive
        )

    @mcp.tool(annotations=READ)
    async def list_when_to_leave_plans(
        flight_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Read this account's saved departure plans through cached, bodyless account sync.

        Excludes deletion markers and other owners. Returns planId, flight, inputs,
        derived leave/traffic/monitoring state and lifecycle. Stopped/expired plans
        remain visible. Optionally filter by flight UUID. limit 1..200.
        """
        return await service.list_when_to_leave_plans(
            resolve_token(ctx), flight_id=flight_id, limit=limit, offset=offset, refresh=refresh
        )

    @mcp.tool(annotations=WRITE)
    async def save_when_to_leave_plan(
        plan_id: str,
        flight_id: str,
        inputs: dict[str, Any],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Save complete When to Leave inputs for a plan and ONLINE flight. Requires writes enabled.

        Use the existing planId when editing. Inspect RemoteWhenToLeavePlan.Inputs
        with get_schema for travel mode, origin/destination, airport allowance,
        buffers, traffic updates and reminders. Sends SAVE through account sync;
        this is a complete input replacement, not a patch. Creation/entitlements
        are not live-validated. Read the plan back after acknowledgment.
        """
        return await api.apply_changes(
            resolve_token(ctx),
            [
                {
                    "whenToLeavePlanChangeAction": {
                        "planId": plan_id,
                        "flight": {"kind": "ONLINE", "flightId": uuid_text(flight_id)},
                        "operation": "SAVE",
                        "inputs": inputs,
                    }
                }
            ],
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def change_when_to_leave_plan(
        plan_id: str,
        operation: Literal[
            "DISABLE", "DELETE", "REQUEST_REFRESH", "SIMULATE_TRAFFIC", "RESET_TRAFFIC_ALERT"
        ],
        flight_id: str | None = None,
        simulated_traffic_seconds: int | None = None,
        confirm_destructive: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Send a departure-plan command through account sync. Requires writes enabled.

        Discover the planId with list_when_to_leave_plans. DELETE additionally
        requires confirm_destructive=true. DISABLE stops the plan; REQUEST_REFRESH
        asks the server to refresh it. SIMULATE_TRAFFIC/RESET_TRAFFIC_ALERT are
        compiled diagnostic commands with unverified production availability.
        Never use them for ordinary refreshes. Acknowledgments need readback.
        """
        body: dict[str, Any] = {"planId": plan_id, "operation": operation}
        if flight_id is not None:
            body["flight"] = {"kind": "ONLINE", "flightId": uuid_text(flight_id)}
        if simulated_traffic_seconds is not None:
            body["simulatedTrafficSeconds"] = simulated_traffic_seconds
        return await api.apply_changes(
            resolve_token(ctx),
            [{"whenToLeavePlanChangeAction": body}],
            confirm_destructive=confirm_destructive,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def remove_profile_avatar(
        confirm_destructive: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Remove this account's profile photo. Requires writes and confirm_destructive=true.

        Uses the dedicated 4.12 sync action. Does not upload a blank image.
        """
        return await api.apply_changes(
            resolve_token(ctx),
            [{"profileRemoveAvatarAction": {}}],
            confirm_destructive=confirm_destructive,
        )

    @mcp.tool(annotations=WRITE)
    async def record_passport_shared(
        year: int, occurred_at: str | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Record an explicitly requested Passport share event for a year, using account sync.

        Requires writes enabled. occurred_at is RFC3339; defaults to now. This
        does not render an image or send it anywhere. Never submit automatically
        when reading statistics or preparing an image.
        """
        return await api.apply_changes(
            resolve_token(ctx),
            [
                {
                    "passportSharedAction": {
                        "year": year,
                        "occurredAt": occurred_at if occurred_at is not None else utc_now(),
                    }
                }
            ],
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_flight(
        flight_id: str, confirmation: str, manual: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Remove a flight from this account. Requires writes enabled and confirmation equal to flight_id.

        This is not archive. manual=true selects deleteManualFlightAction. Uses the
        authenticated account as implicit owner, omitting ownerUserId for own flights.
        """
        if confirmation != flight_id:
            raise FlightyError("confirmation must exactly equal flight_id.")
        token = resolve_token(ctx)
        action = "deleteManualFlightAction" if manual else "flightDeleteAction"
        return await api.apply_changes(
            token,
            [{action: {"flightId": uuid_text(flight_id)}}],
            confirm_destructive=True,
        )

    @mcp.tool(annotations=WRITE)
    async def set_flight_archived(
        flight_id: str, archived: bool, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Archive/unarchive a tracked flight, preserving it in account history. Requires writes enabled."""
        token = resolve_token(ctx)
        action = "flightArchiveAction" if archived else "flightUnarchiveAction"
        return await api.apply_changes(
            token,
            [{action: {"flightId": uuid_text(flight_id), "ownerUserId": token_subject(token)}}],
        )

    @mcp.tool(annotations=WRITE)
    async def set_flight_mine(
        flight_id: str, is_mine: bool, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Mark/unmark a tracked flight as My Flight. Requires writes enabled."""
        action = "flightSetMineAction" if is_mine else "flightUnsetMineAction"
        return await api.apply_changes(
            resolve_token(ctx), [{action: {"flightId": uuid_text(flight_id)}}]
        )

    @mcp.tool(annotations=WRITE)
    async def update_ticket(
        flight_id: str, fields: dict[str, Any], ctx: Context | None = None
    ) -> dict[str, Any]:
        """Update booking reference/seat/cabin/reason while preserving known existing ticket fields.

        fields accepts pnr, seatNumber, seatPosition, cabinClass, flightReason. Inspect
        RemoteTicket enums with get_schema. Reads fresh ticket data before merging the
        provided fields; does not claim server patch semantics. Requires writes enabled.
        """
        return await service.update_ticket(resolve_token(ctx), flight_id, fields)

    @mcp.tool(annotations=WRITE)
    async def set_flight_note(
        flight_id: str, note: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Set a flight note; an empty note explicitly clears it. Requires writes enabled."""
        return await api.apply_changes(
            resolve_token(ctx),
            [
                {
                    "userFlightNoteUploadChangeAction": {
                        "flightId": uuid_text(flight_id),
                        "note": note,
                    }
                }
            ],
        )

    @mcp.tool(annotations=WRITE)
    async def upsert_manual_flight(
        flight: dict[str, Any], ctx: Context | None = None
    ) -> dict[str, Any]:
        """Create/edit a manual flight using a complete ManualFlightChangeAction payload.

        Requires writes enabled. Required local identifiers: flightId, departureAirportId,
        arrivalScheduledAirportId, arrivalActualAirportId. Inspect get_schema for all
        time/equipment/airline fields. gateOriginal/Actual and runwayOriginal/Actual are
        timestamps, not gate/runway names. Omitted fields may clear existing values.
        """
        for name in (
            "flightId",
            "departureAirportId",
            "arrivalScheduledAirportId",
            "arrivalActualAirportId",
        ):
            if name not in flight:
                raise FlightyError(
                    "Supply all four required manual-flight identifiers; see tool description."
                )
            uuid_text(flight[name])
        return await api.apply_changes(resolve_token(ctx), [{"manualFlightChangeAction": flight}])

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_account(
        confirmation_user_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Permanently delete this Flighty account. This is separate from removing a flight.

        Requires both FLIGHTY_ALLOW_WRITES=true and FLIGHTY_ALLOW_ACCOUNT_DELETION=true,
        plus confirmation_user_id exactly matching the authenticated token's subject.
        Never use for cleanup or testing. Live behavior has deliberately not been tested.
        """
        token = resolve_token(ctx)
        if confirmation_user_id != token_subject(token):
            raise FlightyError("Account deletion confirmation does not match this account.")
        return await api.request("delete_account", token)

    def register_endpoint_tool(operation: str) -> None:
        endpoint = ENDPOINTS[operation]
        path_keys = ", ".join(re.findall(r"{(\w+)}", endpoint.path))
        description = (
            f"{operation}: {endpoint.method} {endpoint.path}. "
            + (
                "Read-only lookup. "
                if endpoint.read_only
                else "Mutating; requires FLIGHTY_ALLOW_WRITES=true. Not live-validated. "
            )
            + (f"payload must match get_schema('{endpoint.request}'). " if endpoint.request else "")
            + (
                "payload is the JSON object documented in docs/api.md. "
                if endpoint.json_body
                else ""
            )
            + (f"path keys: {path_keys}. " if path_keys else "No path parameters. ")
            + endpoint.notes
        )

        async def endpoint_tool(
            payload: dict[str, Any] | None = None,
            path: dict[str, str] | None = None,
            query: dict[str, str] | None = None,
            confirm_destructive: bool = False,
            ctx: Context | None = None,
        ) -> dict[str, Any]:
            if endpoint.destructive and not confirm_destructive:
                raise FlightyError(
                    "This operation requires confirm_destructive=true in addition to writes enabled."
                )
            if query and operation not in {"import_calendar", "subscribe_shared_flights"}:
                raise FlightyError("This tool does not accept query parameters.")
            if query:
                allowed = (
                    {"source"} if operation == "import_calendar" else {"source", "is_passenger"}
                )
                if set(query) - allowed:
                    raise FlightyError("Unsupported query parameter for operation.")
                if operation == "import_calendar" and query.get("source") not in {
                    None,
                    "initialSync",
                    "periodicSync",
                    "calendarChanges",
                    "silentPush",
                }:
                    raise FlightyError("Unsupported calendar source variant.")
            if operation == "submit_attribution" and (
                not payload
                or set(payload) != {"token", "service"}
                or payload.get("service") != "ASA"
                or not isinstance(payload.get("token"), str)
            ):
                raise FlightyError("Attribution payload requires string token and service='ASA'.")
            if operation == "mark_year_in_review_viewed" and (
                not payload
                or set(payload) != {"year"}
                or not isinstance(payload.get("year"), str)
                or not payload["year"].isdecimal()
            ):
                raise FlightyError("Year-in-review payload requires year as a digit string.")
            if operation == "create_share_link":
                items = payload.get("items") if payload else None
                if (
                    not isinstance(items, list)
                    or not items
                    or len(items) > 100
                    or set(payload or {}) != {"items"}
                ):
                    raise FlightyError(
                        "Share payload requires 1..100 items containing Flighty sharing URLs."
                    )
                for item in items:
                    if (
                        not isinstance(item, str)
                        or urlsplit(item).scheme != "https"
                        or urlsplit(item).netloc != "live.flighty.app"
                        or urlsplit(item).query
                        or urlsplit(item).fragment
                    ):
                        raise FlightyError(
                            "Only HTTPS live.flighty.app sharing URLs without query/fragment are accepted."
                        )
            return await api.request(
                operation, resolve_token(ctx), payload=payload, path=path, params=query
            )

        mcp.add_tool(
            endpoint_tool,
            name=operation,
            description=description,
            annotations=READ
            if endpoint.read_only
            else DESTRUCTIVE
            if endpoint.destructive
            else WRITE,
        )

    implemented = {
        "search_flights",
        "get_flight",
        "poll_flight",
        "subscribe_flight",
        "subscribe_random_flight",
        "sync_account",
        "apply_changes",
        "delete_account",
    }
    for operation in sorted(ENDPOINTS.keys() - implemented):
        register_endpoint_tool(operation)

    @mcp.tool(annotations=READ)
    async def wait_for_sync(
        timeout_seconds: int = 20, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Wait once for a websocket requestSync notification; does not change account data.

        Registers the bearer with Flighty's websocket session, waits 1..30 seconds,
        and invalidates the local account cache if an update arrives. Does not return
        the bearer or subscribe/add any flight. Avoid loops; rate limits are unknown.
        """
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        if not 1 <= timeout_seconds <= 30:
            raise FlightyError("timeout_seconds must be 1..30.")
        token = resolve_token(ctx)
        try:
            async with asyncio.timeout(timeout_seconds):
                async with connect(
                    WEBSOCKET_URL, proxy=None, open_timeout=10, max_size=1024 * 1024
                ) as socket:
                    await socket.send(
                        proto.encode(
                            "FlightyWebsocketsClientMessage",
                            {"registerSession": {"tokens": [token]}},
                        )
                    )
                    async for message in socket:
                        if not isinstance(message, bytes):
                            continue
                        event = proto.decode("FlightyWebsocketsServerMessage", message)
                        if "requestSync" in event:
                            api.invalidate(token)
                            return {"sync_requested": True}
            return {"sync_requested": False}
        except TimeoutError:
            return {"sync_requested": False, "timed_out": True}
        except (OSError, WebSocketException):
            raise FlightyError("Flighty websocket request failed.") from None

    @mcp.resource(
        "flighty://flights/{when}",
        name="Flights",
        description="Cached account flights: all, upcoming, or past. First 200 records; use list_flights for pagination.",
        mime_type="application/json",
    )
    async def flights_resource(when: str, ctx: Context | None = None) -> str:
        return json.dumps(await service.list_flights(resolve_token(ctx), when=when, limit=200))

    @mcp.resource(
        "flighty://account/{kind}",
        name="Account records",
        description="First 200 records of a supported sync category, with credentials redacted.",
        mime_type="application/json",
    )
    async def account_resource(kind: str, ctx: Context | None = None) -> str:
        return json.dumps(await service.account_records(resolve_token(ctx), kind, limit=200))

    @mcp.resource(
        "flighty://schema/{name}",
        name="Protobuf schema",
        description="Local supported message/enum field contract.",
        mime_type="application/json",
    )
    def schema_resource(name: str) -> str:
        return json.dumps(proto.describe(name))

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Unofficial Flighty MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("FLIGHTY_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("FLIGHTY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLIGHTY_PORT", "8000")))
    parser.add_argument("--path", default=os.environ.get("FLIGHTY_PATH", "/mcp"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    # httpx INFO logging includes opaque sync cursors. Do not enable it.
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        mcp = create_server()
        if args.transport == "stdio":
            mcp.run()
        else:
            mcp.run(
                "streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.path,
                stateless_http=True,
                json_response=True,
            )
    except FlightyError as error:
        parser.exit(1, str(error) + "\n")
