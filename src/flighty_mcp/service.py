"""Account views and common flight edits over the API client."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import proto
from .client import FlightyClient, Snapshot, timestamp, uuid_text
from .config import MIN_SYNC_INTERVAL
from .errors import FlightyError


def local_time(value: str | None, timezone: str | None) -> str | None:
    if not value:
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return timestamp(value).astimezone(zone).isoformat()


def flight_summary(
    record: dict[str, Any], snapshot: Snapshot, *, manual: bool = False
) -> dict[str, Any]:
    flight = record if manual else record.get("flight", {})
    airports = {item["id"]: item for item in snapshot.items("remoteAirport")}
    airlines = {item["id"]: item for item in snapshot.items("remoteAirline")}
    departure, arrival = flight.get("departure", {}), flight.get("arrival", {})
    origin = airports.get(departure.get("airportId"), departure.get("airport", {}))
    destination = airports.get(
        arrival.get("actualAirportId") or arrival.get("scheduledAirportId"),
        arrival.get("actualAirport") or arrival.get("scheduledAirport", {}),
    )
    airline = airlines.get(flight.get("airlineId"), flight.get("airline", {}))
    depart_schedule, arrive_schedule = departure.get("schedule", {}), arrival.get("schedule", {})
    number = flight.get("flightNumber", "")
    scheduled = depart_schedule.get("gateOriginal")
    return {
        "id": record["id"],
        "manual": manual,
        "owner_user_id": record.get("userId"),
        "flight_number": (
            (airline.get("iata") or airline.get("icao") or "") + " " + number
        ).strip(),
        "airline": airline.get("name"),
        "airline_id": flight.get("airlineId"),
        "origin": {
            "id": departure.get("airportId"),
            "iata": origin.get("iata"),
            "name": origin.get("name"),
            "timezone": origin.get("timezone"),
        },
        "destination": {
            "id": arrival.get("actualAirportId") or arrival.get("scheduledAirportId"),
            "iata": destination.get("iata"),
            "name": destination.get("name"),
            "timezone": destination.get("timezone"),
        },
        "departure_scheduled": scheduled,
        "departure_local": local_time(scheduled, origin.get("timezone")),
        "departure_estimated": depart_schedule.get("gateEstimated"),
        "departure_actual": depart_schedule.get("gateActual"),
        "arrival_scheduled": arrive_schedule.get("gateOriginal"),
        "arrival_local": local_time(
            arrive_schedule.get("gateOriginal"), destination.get("timezone")
        ),
        "arrival_estimated": arrive_schedule.get("gateEstimated"),
        "arrival_actual": arrive_schedule.get("gateActual"),
        "departure_gate": departure.get("gate"),
        "arrival_gate": arrival.get("gate"),
        "departure_terminal": departure.get("terminal"),
        "arrival_terminal": arrival.get("terminal"),
        "canceled": flight.get("isCanceled", False),
        "archived": record.get("archived", False),
        "is_mine": record.get("isMine", False),
        "distance_km": flight.get("distance", flight.get("distanceKm")),
        "equipment": flight.get("equipment"),
        "updated": record.get("updated"),
    }


def cache_info(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "fetched_at": snapshot.fetched_at,
        "pages": snapshot.pages,
        "minimum_refresh_interval_seconds": MIN_SYNC_INTERVAL,
        "refresh_limited": snapshot.refresh_limited,
        "schema_unknown_fields_present": snapshot.unknown_fields,
    }


class FlightyService:
    def __init__(self, client: FlightyClient) -> None:
        self.client = client

    async def list_flights(
        self,
        token: str,
        *,
        when: str = "all",
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
    ) -> dict[str, Any]:
        if when not in {"all", "upcoming", "past"} or not 1 <= limit <= 200 or offset < 0:
            raise FlightyError(
                "Invalid list filter/pagination; limit must be 1..200 and offset nonnegative."
            )
        for date in (start_date, end_date):
            if date:
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    raise FlightyError("Dates must use YYYY-MM-DD.") from None
        if start_date and end_date and start_date > end_date:
            raise FlightyError("start_date must not follow end_date.")
        snapshot = await self.client.snapshot(token, refresh=refresh)
        normal = [(record, False) for record in snapshot.owned("remoteUserFlight")]
        normal.extend((record, True) for record in snapshot.owned("remoteUserManualFlight"))
        found = []
        now = datetime.now(UTC)
        for record, manual in normal:
            summary = flight_summary(record, snapshot, manual=manual)
            dep = summary["departure_scheduled"]
            dep_time = timestamp(dep) if dep else None
            if mine_only and not summary["is_mine"]:
                continue
            if archived is not None and summary["archived"] != archived:
                continue
            if when == "upcoming" and (not dep_time or dep_time < now):
                continue
            if when == "past" and (not dep_time or dep_time >= now):
                continue
            if origin and origin.casefold() not in str(summary["origin"]).casefold():
                continue
            if destination and destination.casefold() not in str(summary["destination"]).casefold():
                continue
            local_date = (summary["departure_local"] or "")[:10]
            if start_date and local_date < start_date:
                continue
            if end_date and (not local_date or local_date > end_date):
                continue
            if details:
                summary["record"] = record
            found.append(summary)
        found.sort(key=lambda item: (item["departure_scheduled"] or "9999", item["id"]))
        return {
            "total": len(found),
            "count": len(found[offset : offset + limit]),
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < len(found) else None,
            "flights": found[offset : offset + limit],
            "cache": cache_info(snapshot),
        }

    async def account_records(
        self, token: str, kind: str, *, limit: int = 50, offset: int = 0, refresh: bool = False
    ) -> dict[str, Any]:
        kinds = [f["json_name"] for f in proto.SCHEMA["SyncResponseData"]["fields"]]
        if kind not in kinds or not 1 <= limit <= 200 or offset < 0:
            raise FlightyError(
                "Invalid record kind/limit/offset. Use get_schema('SyncResponseData') for kinds."
            )
        snapshot = await self.client.snapshot(token, refresh=refresh)
        records = (
            snapshot.owned(kind)
            if kind in {"remoteUserFlight", "remoteUserManualFlight", "remoteWhenToLeavePlan"}
            else snapshot.items(kind)
        )
        return {
            "kind": kind,
            "total": len(records),
            "records": records[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(records) else None,
            "cache": cache_info(snapshot),
        }

    async def list_when_to_leave_plans(
        self,
        token: str,
        *,
        flight_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200 or offset < 0:
            raise FlightyError("limit must be 1..200 and offset nonnegative.")
        if flight_id is not None:
            flight_id = uuid_text(flight_id)
        snapshot = await self.client.snapshot(token, refresh=refresh)
        plans = [
            item
            for item in snapshot.owned("remoteWhenToLeavePlan")
            if flight_id is None or item.get("flight", {}).get("flightId") == flight_id
        ]
        plans.sort(key=lambda item: str(item.get("planId", "")))
        return {
            "total": len(plans),
            "plans": plans[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(plans) else None,
            "cache": cache_info(snapshot),
        }

    async def search_catalog(
        self, token: str, kind: str, query: str, limit: int = 20
    ) -> dict[str, Any]:
        if (
            kind not in {"remoteAirport", "remoteAirline", "remoteAircraftType", "remoteMetroArea"}
            or not 1 <= limit <= 100
        ):
            raise FlightyError("Unsupported catalog or limit.")
        snapshot = await self.client.snapshot(token)
        needle = query.strip().casefold()
        matches = [
            item
            for item in snapshot.items(kind)
            if needle
            in " ".join(
                str(item.get(key, ""))
                for key in ("id", "name", "iata", "icao", "city", "country", "modelName")
            ).casefold()
        ]
        matches.sort(
            key=lambda item: (
                str(item.get("iata", "")).casefold() != needle,
                str(item.get("name", "")),
            )
        )
        return {"total": len(matches), "results": matches[:limit], "cache": cache_info(snapshot)}

    async def update_ticket(
        self, token: str, flight_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        self.client.require_writes()
        uuid_text(flight_id)
        allowed = {"pnr", "seatNumber", "seatPosition", "cabinClass", "flightReason"}
        if not fields or set(fields) - allowed or any(value is None for value in fields.values()):
            raise FlightyError(
                "Supply non-null ticket fields: pnr, seatNumber, seatPosition, cabinClass, flightReason."
            )
        snapshot = await self.client.snapshot(token, refresh=True, fresh_for_write=True)
        tickets = [
            item
            for item in snapshot.items("remoteTicket")
            if item.get("flightId") == flight_id
            and item.get("userId", snapshot.account_id) == snapshot.account_id
        ]
        payload = {
            key: value for key, value in (tickets[0] if tickets else {}).items() if key in allowed
        }
        payload.update(copy.deepcopy(fields))
        payload["flightId"] = flight_id
        return await self.client.apply_changes(token, [{"ticketInfoAction": payload}])
