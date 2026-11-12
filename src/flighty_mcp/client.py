"""Flighty API transport, read-only sync, bounded per-credential cache and edits."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import UUID
from weakref import WeakValueDictionary

import httpx

from . import proto
from .capabilities import capability_header, client_capabilities
from .config import API_BASE, MIN_SYNC_INTERVAL, SCHEMA_VERSION, Settings
from .endpoints import ENDPOINTS
from .errors import FlightyError, WritesDisabled


def validate_token(token: str) -> str:
    token = token.strip()
    if not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise FlightyError("A single Flighty bearer credential is required.")
    return token


def token_subject(token: str) -> str:
    """Read an identity hint, never a signature verification or authorization check."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(UUID(payload["sub"]))
    except (IndexError, ValueError, KeyError, TypeError):
        raise FlightyError(
            "Cannot determine account ID from credential. Expected Flighty's JWT token."
        ) from None


def uuid_text(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise FlightyError("Expected a UUID identifier.") from None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise FlightyError("Expected an RFC3339 timestamp with a timezone.") from None


def validate_plan_change(body: dict[str, Any]) -> str:
    """Validate nested commands before the generic sync mutation path."""
    plan_id = body.get("planId")
    if (
        not isinstance(plan_id, str)
        or not plan_id.strip()
        or len(plan_id) > 512
        or any(ord(c) < 32 for c in plan_id)
    ):
        raise FlightyError("Supply a nonempty planId of at most 512 characters.")
    values = proto.SCHEMA["SyncChangeRequestProto.WhenToLeavePlanChangeAction.Operation"]["values"]
    operation = body.get("operation")
    if type(operation) is int:
        operation = next((name for name, value in values.items() if value == operation), None)
    if not isinstance(operation, str) or operation not in values or operation == "UNKNOWN":
        raise FlightyError("Supply a supported When to Leave operation; UNKNOWN is not a command.")
    flight = body.get("flight")
    if flight is not None:
        if (
            not isinstance(flight, dict)
            or type(flight.get("kind")) not in {str, int}
            or flight.get("kind") not in ("ONLINE", 1)
        ):
            raise FlightyError("When to Leave flight references must have kind ONLINE.")
        uuid_text(flight.get("flightId", ""))
    if operation == "SAVE" and (
        flight is None or not isinstance(body.get("inputs"), dict) or not body["inputs"]
    ):
        raise FlightyError("Saving a plan requires an ONLINE flight and complete, nonempty inputs.")
    if "simulatedTrafficSeconds" in body and body["simulatedTrafficSeconds"] is not None:
        seconds = body["simulatedTrafficSeconds"]
        if operation != "SIMULATE_TRAFFIC" or (
            type(seconds) is not int
            and not (isinstance(seconds, str) and len(seconds) <= 20 and seconds.isdecimal())
        ):
            raise FlightyError(
                "simulatedTrafficSeconds requires SIMULATE_TRAFFIC and nonnegative seconds."
            )
        if int(seconds) < 0:
            raise FlightyError("simulatedTrafficSeconds must be nonnegative.")
    return operation


@dataclass
class Snapshot:
    account_id: str
    records: dict[str, list[dict[str, Any]]]
    pages: int
    fetched_at: str
    stored_at: float = field(default_factory=time.monotonic)
    unknown_fields: bool = False
    refresh_limited: bool = False

    def items(self, kind: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        records = self.records.get(kind, [])
        return [
            item
            for item in records
            if include_deleted
            or (
                not item.get("deleted")
                and not (kind == "remoteWhenToLeavePlan" and item.get("lifecycle") == "DELETED")
            )
        ]

    def owned(self, kind: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        return [
            item
            for item in self.items(kind, include_deleted=include_deleted)
            if item.get("userId") == self.account_id
        ]


class FlightyClient:
    """A reusable API client. No global bearer credential is retained."""

    def __init__(
        self, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings or Settings.from_env()
        # HTTPX's INFO request line includes opaque account pagination cursors.
        # Protect library callers as well as the CLI's logging configuration.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.transport = transport
        self._cache: OrderedDict[str, Snapshot] = OrderedDict()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def invalidate(self, token: str) -> None:
        self._cache.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def require_writes(self) -> None:
        if not self.settings.allow_writes:
            raise WritesDisabled(
                "Writes are disabled. Set FLIGHTY_ALLOW_WRITES=true to enable mutations."
            )

    def _headers(self, token: str) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/x-protobuf",
            "Content-Type": "application/x-protobuf",
            "Accept-Encoding": "gzip",
            "User-Agent": self.settings.user_agent,
            "X-Flighty-Locale": self.settings.locale,
            "X-Flighty-Client-Capabilities": capability_header(),
        }
        if self.settings.build_token:
            headers["X-Flighty-Build-Token"] = self.settings.build_token
        if self.settings.device_id:
            headers["Device"] = self.settings.device_id
        return headers

    async def request(
        self,
        operation: str,
        token: str,
        *,
        path: dict[str, str] | None = None,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if operation not in ENDPOINTS:
            raise FlightyError("Unknown Flighty operation.")
        endpoint = ENDPOINTS[operation]
        if not endpoint.read_only:
            self.require_writes()
        if operation == "delete_account" and not self.settings.allow_account_deletion:
            raise WritesDisabled("Account deletion is disabled separately from ordinary writes.")
        if endpoint.read_only and endpoint.request is None and payload is not None:
            raise FlightyError("This download operation must not contain a request body.")
        parts = path or {}
        expected = set(re.findall(r"\{(\w+)\}", endpoint.path))
        if set(parts) != expected:
            raise FlightyError("Incorrect path parameters for operation.")
        for key, value in parts.items():
            if key in {"flight_id", "airport_id", "user_id"}:
                uuid_text(value)
            elif key in {"share_id", "invite_id"} and not re.fullmatch(
                r"[A-Za-z0-9._~-]{1,512}", value
            ):
                raise FlightyError("Invalid sharing/invitation identifier.")
            elif key == "direction" and value not in {"DEPARTURE", "ARRIVAL"}:
                raise FlightyError("Direction must be DEPARTURE or ARRIVAL.")
            elif key == "code" and (
                not value or len(value) > 200 or any(c in value for c in "\r\n\0/\\?#")
            ):
                raise FlightyError("Invalid promotion code.")
        route = endpoint.path.format(**{key: quote(value, safe="") for key, value in parts.items()})
        if headers and set(headers) - {"If-None-Match", "X-Sync-Priority"}:
            raise FlightyError("Unsupported request header override.")
        request_headers = self._headers(token)
        request_headers.update(headers or {})
        if operation == "poll_flight":
            request_headers.setdefault("X-Sync-Priority", "0")
        content: bytes | None = None
        if endpoint.request:
            if payload is None:
                raise FlightyError(f"{operation} requires a payload matching {endpoint.request}.")
            if operation == "register_device":
                payload = {"capabilities": client_capabilities(), **payload}
            content = proto.encode(endpoint.request, payload)
        elif endpoint.json_body:
            if payload is None:
                raise FlightyError("This operation requires a JSON body.")
            content = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Accept"] = "application/json"
        elif payload is not None:
            raise FlightyError("Unexpected body for operation.")
        try:
            async with httpx.AsyncClient(  # noqa: SIM117 -- stream lifetime is inside client lifetime
                transport=self.transport,
                timeout=self.settings.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                async with http.stream(
                    endpoint.method,
                    API_BASE + route,
                    params=urlencode(params) if params is not None else None,
                    headers=request_headers,
                    content=content,
                ) as response:
                    status = response.status_code
                    if status == 304:
                        return {"not_modified": True, "status": status}
                    if status < 200 or status >= 300:
                        retry = response.headers.get("retry-after", "")
                        suffix = f" Retry after {retry} seconds." if retry.isdecimal() else ""
                        if status == 429:
                            raise FlightyError("Flighty API rate limit reached." + suffix)
                        if status in {401, 403}:
                            raise FlightyError(
                                f"Flighty API rejected authentication/access ({status}). Check account and app build tokens."
                            )
                        raise FlightyError(
                            f"Flighty API returned HTTP {status} for {operation}; request was not retried."
                        )
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_response_bytes:
                            raise FlightyError(
                                "Flighty response exceeded the configured size limit."
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    etag = response.headers.get("etag")
        except httpx.HTTPError:
            raise FlightyError(
                "Flighty API network/TLS request failed; it was not retried."
            ) from None
        if not endpoint.read_only:
            self.invalidate(token)
        meta: dict[str, Any] = {
            "status": status,
            "operation": operation,
            "schema_version": SCHEMA_VERSION,
            "rate_limits": "undocumented",
        }
        if etag:
            meta["etag"] = etag
        if endpoint.binary_response:
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/heic"}:
                raise FlightyError("Avatar endpoint returned an unexpected content type.")
            return {
                "mime_type": content_type,
                "base64": base64.b64encode(data).decode(),
                "meta": meta,
            }
        if not data:
            return {"data": {}, "meta": meta}
        if endpoint.json_body:
            try:
                parsed = json.loads(data)
            except (ValueError, UnicodeError):
                raise FlightyError("Flighty returned invalid JSON.") from None
            return {"data": proto.redact(parsed), "meta": meta}
        if endpoint.response:
            if content_type not in {
                "application/x-protobuf",
                "application/protobuf",
                "application/octet-stream",
            }:
                raise FlightyError("Flighty returned an unexpected response content type.")
            message = proto.parse(endpoint.response, data)
            meta["unknown_fields_present"] = proto.unknown_fields_present(message)
            decoded = proto.as_dict(message)
            if operation == "search_flights" and "error" in decoded:
                raise FlightyError("Flighty API search rate limit reached.")
            return {"data": proto.redact(decoded), "meta": meta}
        # The app ignores bodies on several acknowledgment endpoints; do likewise.
        return {"data": {}, "meta": meta}

    async def snapshot(
        self, token: str, *, refresh: bool = False, fresh_for_write: bool = False
    ) -> Snapshot:
        if fresh_for_write:
            self.require_writes()
        token = validate_token(token)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()

        def cached_result() -> Snapshot | None:
            cached = self._cache.get(fingerprint)
            if cached is None or fresh_for_write:
                return None
            age = max(0.0, time.monotonic() - cached.stored_at)
            limited = age <= MIN_SYNC_INTERVAL
            if limited or (not refresh and age < self.settings.cache_ttl):
                self._cache.move_to_end(fingerprint)
                result = copy.deepcopy(cached)
                result.refresh_limited = refresh and limited
                return result
            return None

        if (cached := cached_result()) is not None:
            return cached
        # Only fingerprints, never raw keys, are held as cache/lock identifiers.
        lock = self._locks.setdefault(fingerprint, asyncio.Lock())
        async with lock:
            if (cached := cached_result()) is not None:
                return cached
            snapshot = await self._download(token)
            self._cache[fingerprint] = snapshot
            self._cache.move_to_end(fingerprint)
            while len(self._cache) > self.settings.max_cache_accounts:
                old, _ = self._cache.popitem(last=False)
                if old != fingerprint and old in self._locks and not self._locks[old].locked():
                    self._locks.pop(old, None)
            return copy.deepcopy(snapshot)

    async def _download(self, token: str) -> Snapshot:
        account_id = token_subject(token)
        records: dict[str, dict[tuple[str, ...], dict[str, Any]]] = defaultdict(dict)
        cursor: str | None = None
        seen: set[str] = set()
        unknown = False
        for page in range(1, self.settings.max_pages + 1):
            query: list[tuple[str, str]] | None = None
            if cursor:
                if cursor in seen or len(cursor) > 16384:
                    raise FlightyError(
                        "Flighty pagination loop or oversized cursor; incomplete download was not cached."
                    )
                seen.add(cursor)
                # Match the app: copy query items; never request a server-supplied host/path.
                query = parse_qsl(urlsplit(cursor).query, keep_blank_values=True)
                if not query:
                    raise FlightyError(
                        "Flighty pagination cursor has no query; incomplete download was not cached."
                    )
            result = await self.request("sync_account", token, params=query)
            unknown |= result["meta"].get("unknown_fields_present", False)
            payload = result["data"]
            for wrapper in payload.get("data", []):
                for kind, item in wrapper.items():
                    if not isinstance(item, dict):
                        continue
                    # ID alone is insufficient: friend records can share the same flight ID.
                    key: tuple[str, ...]
                    if item.get("id") or item.get("planId"):
                        key = (str(item.get("userId", "")), str(item.get("id") or item["planId"]))
                    elif item.get("flightId"):
                        key = (str(item.get("userId", "")), str(item["flightId"]))
                    else:
                        key = (json.dumps(item, sort_keys=True),)
                    previous = records[kind].get(key)
                    if (
                        previous
                        and previous.get("updated")
                        and item.get("updated")
                        and timestamp(previous["updated"]) > timestamp(item["updated"])
                    ):
                        continue
                    records[kind][key] = item
            pagination = payload.get("pagination", {})
            if not pagination.get("moreData", False):
                return Snapshot(
                    account_id,
                    {kind: list(items.values()) for kind, items in records.items()},
                    page,
                    utc_now(),
                    unknown_fields=unknown,
                )
            cursor = pagination.get("nextUrl")
            if not isinstance(cursor, str) or not cursor:
                raise FlightyError(
                    "Flighty omitted its next-page cursor; incomplete download was not cached."
                )
        raise FlightyError(
            "Flighty pagination exceeded max-pages; incomplete download was not cached."
        )

    async def apply_changes(
        self, token: str, changes: list[dict[str, Any]], *, confirm_destructive: bool = False
    ) -> dict[str, Any]:
        self.require_writes()
        if not changes or len(changes) > 100:
            raise FlightyError("Supply between 1 and 100 sync changes.")
        owner = token_subject(token)
        allowed = {
            f["json_name"]
            for f in proto.SCHEMA["SyncChangeRequestProto"]["fields"]
            if f.get("oneof") == "change"
        }
        prepared = copy.deepcopy(changes)
        for change in prepared:
            actions = [key for key in change if key != "timestamp"]
            if len(actions) != 1 or actions[0] not in allowed:
                raise FlightyError(
                    "Every sync change needs exactly one supported action; use get_schema."
                )
            action = actions[0]
            body = change[action]
            if not isinstance(body, dict):
                raise FlightyError("Each action payload must be a JSON object.")
            if body.get("ownerUserId") and body["ownerUserId"] != owner:
                raise FlightyError("Changing a different owner's data is not supported.")
            plan_operation = ""
            if action == "whenToLeavePlanChangeAction":
                plan_operation = validate_plan_change(body)
            if action == "deviceChangeAction":
                body.setdefault("capabilities", client_capabilities())
            if action == "passportSharedAction":
                year = body.get("year")
                if type(year) is not int or not 1 <= year <= 9999:
                    raise FlightyError("Passport sharing requires a year between 1 and 9999.")
            for key in ("flightId", "ownerUserId", "otherUserId", "airportId", "connectionId"):
                if body.get(key):
                    uuid_text(body[key])
            destructive = (
                any(word in action.casefold() for word in ("delete", "remove", "revoke", "bulk"))
                or plan_operation == "DELETE"
            )
            if destructive and not confirm_destructive:
                raise FlightyError("Destructive sync actions require confirm_destructive=true.")
            change.setdefault("timestamp", utc_now())
            timestamp(change["timestamp"])
        # Validate the entire batch before one request. Never retry non-idempotent writes.
        proto.encode("SyncRequestProto", {"changes": prepared})
        result = await self.request("apply_changes", token, payload={"changes": prepared})
        # A write can return an entire account sync page. Forwarding it can exceed
        # MCP transport limits after the write succeeds, leaving its outcome unclear.
        # Counts describe returned records, not which changes were applied. Callers
        # must read the target back to verify; never retry the write automatically.
        counts: dict[str, int] = defaultdict(int)
        for record in result["data"].get("data", []):
            for kind in record:
                counts[kind] += 1
        return {
            "submitted_changes": len(prepared),
            "data": {
                "returned_record_counts": dict(counts),
                "more_data": result["data"].get("pagination", {}).get("moreData", False),
            },
            "readback_required": True,
            "meta": result["meta"],
        }
