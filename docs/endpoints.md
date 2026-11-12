| Tool/operation | HTTP | Access | Request schema | Response schema |
| --- | --- | --- | --- | --- |
| `search_flights` | `POST /v1/search` | Read | `SearchRequestProto` | `SearchResponseProto` |
| `get_flight` | `GET /v1/flight/{flight_id}` | Read | `none` | `GetSharingLinkResponseProto` |
| `subscribe_flight` | `POST /v1/flight/{flight_id}/subscribe` | Write | `none` | `SubscribeFlightResponseProto` |
| `subscribe_random_flight` | `POST /v1/flight/random/subscribe` | Write | `none` | `SubscribeFlightResponseProto` |
| `sync_account` | `POST /v1/sync/full` | Read | `none` | `SyncResponseProto` |
| `apply_changes` | `POST /v1/sync/full` | Write | `SyncRequestProto` | `SyncResponseProto` |
| `poll_flight` | `POST /v1/sync/content/flight/{flight_id}` | Read | `none` | `SyncResponseProto` |
| `subscribe_shared_flights` | `POST /v1/flight/sharing/{share_id}/subscribe` | Write | `none` | `SharingSubscribeFlightResponseProto` |
| `register_anonymous_user` | `POST /v1/user` | Write | `CreateUserRequestProto` | `RemoteUser` |
| `delete_account` | `DELETE /v1/user` | Write | `none` | `acknowledgment/JSON` |
| `register_device` | `POST /v1/device` | Write | `CreateDeviceRequestProto` | `RemoteDeviceProto` |
| `link_account` | `POST /v1/user/link` | Write | `UserLinkRequestProto` | `acknowledgment/JSON` |
| `get_avatar` | `GET /v1/user/{user_id}/avatar` | Read | `none` | `binary image` |
| `upload_receipt` | `POST /v1/receipt` | Write | `ReceiptRequestProto` | `acknowledgment/JSON` |
| `submit_attribution` | `POST /v1/user/attribute` | Write | `JSON` | `acknowledgment/JSON` |
| `mark_year_in_review_viewed` | `POST /v1/user/settings/viewedYearInReview` | Write | `JSON` | `acknowledgment/JSON` |
| `request_export` | `POST /v1/user/export` | Write | `TriggerExportRequestProto` | `acknowledgment/JSON` |
| `request_bulk_import` | `POST /v1/user/triggerBulkImport` | Write | `TriggerBulkImportRequestProto` | `acknowledgment/JSON` |
| `redeem_promo_code` | `POST /v1/user/promocode/{code}/redeem` | Write | `none` | `RedeemPromoCodeResponseProto` |
| `import_calendar` | `POST /v1/sync/calendar` | Write | `CalendarImportRequestProto` | `acknowledgment/JSON` |
| `lookup_aircraft` | `POST /v1/autocomplete/aircraftTypeFromTail` | Read | `AutocompleteAircraftTypeRequestProto` | `AutocompleteAircraftTypeResponseProto` |
| `get_static_assets` | `GET /v1/static_assets` | Read | `none` | `GetStaticAssetsResponse` |
| `get_when_to_leave_holidays` | `GET /v1/when-to-leave/online/{flight_id}/holidays` | Read | `none` | `RemotePublicHolidays` |
| `estimate_when_to_leave` | `POST /v1/when-to-leave/online/{flight_id}/estimate` | Read | `RemoteWhenToLeavePlan.Inputs` | `RemoteWhenToLeavePlan.Estimate` |
| `get_radar_layers` | `GET /v1/radar/layers` | Read | `none` | `GetRadarLayersResponseProto` |
| `get_airport_status` | `GET /v1/airport-status/{airport_id}` | Read | `none` | `RemoteAirportCardResponseProto` |
| `get_airport_mini_status` | `GET /v1/airport-status/{airport_id}/mini` | Read | `none` | `RemoteMiniAirportStatus` |
| `get_airports_home` | `POST /v1/airport-status` | Read | `AirportsHomeRequestProto` | `RemoteAirportsHomeResponseProto` |
| `get_airports_map` | `POST /v1/airport-status/map` | Read | `AirportsHomeMapRequestProto` | `RemoteAirportsHomeMapResponseProto` |
| `get_airport_board` | `GET /v1/airport-board/{airport_id}/{direction}` | Read | `none` | `RemoteAirportBoardResponseProto` |
| `create_share_link` | `POST /v1/share` | Write | `JSON` | `acknowledgment/JSON` |
| `create_friend_invite` | `POST /v1/connected-friends/invite-links` | Write | `ConnectedFriendsInviteLinkAcceptRequestProto` | `ConnectedFriendsInviteLinkResponseProto` |
| `get_friend_invite` | `GET /v1/connected-friends/invite-links/{invite_id}` | Read | `none` | `ConnectedFriendsInviteLinkLookupResponseProto` |
| `accept_friend_invite` | `POST /v1/connected-friends/invite-links/{invite_id}/accept` | Write | `ConnectedFriendsInviteLinkAcceptRequestProto` | `ConnectedFriendsInviteLinkAcceptResponseProto` |
| `start_live_activity` | `POST /v1/flight/{flight_id}/liveActivity` | Write | `StartLiveActivityRequest` | `acknowledgment/JSON` |
