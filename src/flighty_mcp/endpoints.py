"""Closed endpoint catalog. Callers cannot supply arbitrary URLs or HTTP methods."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import APP_CONTRACT


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    read_only: bool
    request: str | None = None
    response: str | None = None
    json_body: bool = False
    binary_response: bool = False
    destructive: bool = False
    notes: str = "Mutation behavior has not been live-tested."


ENDPOINTS = {
    "search_flights": Endpoint(
        "POST", "/v1/search", True, "SearchRequestProto", "SearchResponseProto"
    ),
    "get_flight": Endpoint(
        "GET", "/v1/flight/{flight_id}", True, response="GetSharingLinkResponseProto"
    ),
    "subscribe_flight": Endpoint(
        "POST", "/v1/flight/{flight_id}/subscribe", False, response="SubscribeFlightResponseProto"
    ),
    "subscribe_random_flight": Endpoint(
        "POST", "/v1/flight/random/subscribe", False, response="SubscribeFlightResponseProto"
    ),
    "sync_account": Endpoint(
        "POST",
        "/v1/sync/full",
        True,
        response="SyncResponseProto",
        notes="Body is absent. Copies only query items from server nextUrl. Read-only download path.",
    ),
    "apply_changes": Endpoint(
        "POST", "/v1/sync/full", False, "SyncRequestProto", "SyncResponseProto", destructive=True
    ),
    "poll_flight": Endpoint(
        "POST", "/v1/sync/content/flight/{flight_id}", True, response="SyncResponseProto"
    ),
    "subscribe_shared_flights": Endpoint(
        "POST",
        "/v1/flight/sharing/{share_id}/subscribe",
        False,
        response="SharingSubscribeFlightResponseProto",
    ),
    "register_anonymous_user": Endpoint(
        "POST", "/v1/user", False, "CreateUserRequestProto", "RemoteUser"
    ),
    "delete_account": Endpoint("DELETE", "/v1/user", False, destructive=True),
    "register_device": Endpoint(
        "POST", "/v1/device", False, "CreateDeviceRequestProto", "RemoteDeviceProto"
    ),
    "link_account": Endpoint(
        "POST",
        "/v1/user/link",
        False,
        "UserLinkRequestProto",
        destructive=True,
        notes="Approving account authenticates; body token is the incoming approval token. Initiator token issuance/merge semantics remain unverified.",
    ),
    "get_avatar": Endpoint("GET", "/v1/user/{user_id}/avatar", True, binary_response=True),
    "upload_receipt": Endpoint("POST", "/v1/receipt", False, "ReceiptRequestProto"),
    "submit_attribution": Endpoint("POST", "/v1/user/attribute", False, json_body=True),
    "mark_year_in_review_viewed": Endpoint(
        "POST", "/v1/user/settings/viewedYearInReview", False, json_body=True
    ),
    "request_export": Endpoint("POST", "/v1/user/export", False, "TriggerExportRequestProto"),
    "request_bulk_import": Endpoint(
        "POST", "/v1/user/triggerBulkImport", False, "TriggerBulkImportRequestProto"
    ),
    "redeem_promo_code": Endpoint(
        "POST", "/v1/user/promocode/{code}/redeem", False, response="RedeemPromoCodeResponseProto"
    ),
    "import_calendar": Endpoint(
        "POST", "/v1/sync/calendar", False, "CalendarImportRequestProto", destructive=True
    ),
    "lookup_aircraft": Endpoint(
        "POST",
        "/v1/autocomplete/aircraftTypeFromTail",
        True,
        "AutocompleteAircraftTypeRequestProto",
        "AutocompleteAircraftTypeResponseProto",
    ),
    "get_static_assets": Endpoint(
        "GET", "/v1/static_assets", True, response="GetStaticAssetsResponse"
    ),
    "get_when_to_leave_holidays": Endpoint(
        "GET",
        "/v1/when-to-leave/online/{flight_id}/holidays",
        True,
        response="RemotePublicHolidays",
        notes="Flighty 4.12 departure holiday lookup; availability is not live-validated.",
    ),
    "estimate_when_to_leave": Endpoint(
        "POST",
        "/v1/when-to-leave/online/{flight_id}/estimate",
        True,
        "RemoteWhenToLeavePlan.Inputs",
        "RemoteWhenToLeavePlan.Estimate",
        notes="Preview only; does not save a plan or enable reminders. Availability is not live-validated.",
    ),
    "get_radar_layers": Endpoint(
        "GET", "/v1/radar/layers", True, response="GetRadarLayersResponseProto"
    ),
    "get_airport_status": Endpoint(
        "GET", "/v1/airport-status/{airport_id}", True, response="RemoteAirportCardResponseProto"
    ),
    "get_airport_mini_status": Endpoint(
        "GET", "/v1/airport-status/{airport_id}/mini", True, response="RemoteMiniAirportStatus"
    ),
    "get_airports_home": Endpoint(
        "POST",
        "/v1/airport-status",
        True,
        "AirportsHomeRequestProto",
        "RemoteAirportsHomeResponseProto",
    ),
    "get_airports_map": Endpoint(
        "POST",
        "/v1/airport-status/map",
        True,
        "AirportsHomeMapRequestProto",
        "RemoteAirportsHomeMapResponseProto",
    ),
    "get_airport_board": Endpoint(
        "GET",
        "/v1/airport-board/{airport_id}/{direction}",
        True,
        response="RemoteAirportBoardResponseProto",
    ),
    "create_share_link": Endpoint("POST", "/v1/share", False, json_body=True),
    "create_friend_invite": Endpoint(
        "POST",
        "/v1/connected-friends/invite-links",
        False,
        "ConnectedFriendsInviteLinkAcceptRequestProto",
        "ConnectedFriendsInviteLinkResponseProto",
    ),
    "get_friend_invite": Endpoint(
        "GET",
        "/v1/connected-friends/invite-links/{invite_id}",
        True,
        response="ConnectedFriendsInviteLinkLookupResponseProto",
    ),
    "accept_friend_invite": Endpoint(
        "POST",
        "/v1/connected-friends/invite-links/{invite_id}/accept",
        False,
        "ConnectedFriendsInviteLinkAcceptRequestProto",
        "ConnectedFriendsInviteLinkAcceptResponseProto",
    ),
    "start_live_activity": Endpoint(
        "POST", "/v1/flight/{flight_id}/liveActivity", False, "StartLiveActivityRequest"
    ),
}


def catalog() -> dict[str, Any]:
    return {
        "app_contract": APP_CONTRACT,
        "operations": {name: asdict(endpoint) for name, endpoint in ENDPOINTS.items()},
        "browser_only": {
            "tripit": {
                "method": "GET",
                "path": "/v1/external/tripit",
                "query": {"token": "account bearer", "onComplete": "flighty://tripIt"},
                "notes": "Use the app to launch this browser authorization flow. Never put bearer-bearing URLs into chat/logs.",
            },
        },
        "websocket": {
            "url": "wss://websockets.flightyapp.com/ws",
            "client_message": "FlightyWebsocketsClientMessage",
            "server_message": "FlightyWebsocketsServerMessage",
        },
    }
