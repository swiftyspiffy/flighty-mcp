# Testing

## Offline suite

`uv run pytest` uses synthetic UUIDs, example.invalid addresses, made-up flight
data and programmatically assembled fake JWTs. No fixture comes from an actual
account. All upstream HTTP traffic is intercepted by RESPX; an unregistered route
fails rather than reaching the internet. Websocket access is denied unless a test
explicitly replaces it with an in-memory socket.

The suite checks independent protobuf wire fixtures, every supported mutation tag,
exact headers/bodies, caller isolation, safe error handling, pagination limits,
deletion-marker handling, caches, current-state ticket merges, file permissions,
credential import, MCP discovery/resources and HTTP credential propagation. Mocked
mutation success proves local behavior, not Flighty's current backend semantics.

## Live validation status

Core flight-management functionality passed supervised live tests on disposable
flights, with saved values read back after changes:

- Add a regular flight and delete it.
- Create and edit notes.
- Edit seat and booking reference while preserving other editable ticket values.
- Switch between My Flight and friends, then archive and unarchive.
- Create, edit and delete a manual flight.

Final verification confirmed both disposable flights were absent and every
original flight remained. Flighty may reset a ticket's server-managed `created`
timestamp during editing; preservation applies to editable fields.

Social features and account deletion have not been live-tested. Other operations
with automated/mock coverage only include random-flight additions, imports,
export requests, live activities, account creation/linking, device registration,
receipt uploads, attribution, promotions and year-in-review updates. Advanced
sync actions beyond those core edits and the planner cases below have not been
individually live-tested.
Automated coverage does not establish that the live backend accepts every operation.

Live validation on 2026-11-12 passed on one authorized account: ordinary flight
reads, saved plans, departure holidays, a driving preview, Schengen flags and the
2026 statistics distribution listing/decoder. The distribution decoded without
unknown fields, including 13 global metrics and airline/airport thresholds.

On a disposable upcoming regular flight, a new UUID plan ID was accepted for a
manual plan. Complete-input saves changed the travel buffer and reminder flag;
the derived leave time matched the expected arithmetic. REQUEST_REFRESH was
accepted for that manual plan, DISABLE produced STOPPED, and DELETE removed it
from normal reads. The test plan and flight were deleted. Final verification
preserved every original flight and plan, including the original plan settings.

The successful driving preview used a typed coordinate origin (CURRENT_LOCATION,
UNKNOWN_DESTINATION quality, timezone/country/accuracy), an airport destination
and departure context, with nativePreview omitted. A fabricated MANUAL-origin
payload and a verbatim saved app-state payload returned HTTP 400. Those input
shapes are not interchangeable with a server preview request.

These results cover one account. Avatar removal, Passport share events, traffic
simulation/reset, automatic traffic-alert delivery and actual notification delivery
remain untested. No live responses or credentials were added to test fixtures.

## Read-only live check

```bash
export FLIGHTY_TOKEN_FILE=/absolute/path/to/private-token
uv run python scripts/smoke_test.py
```

This script hard-codes writes and account deletion off, independent of environment
flags. It performs only a bodyless account download, GET flight details and GET
mini airport status. It prints operation status and counts, never credentials or
personal flight details. It is not part of CI and should only run on your own
account or one you have explicit permission to access.

Do not record real responses as fixtures, paste them into issues, or commit live
test output. Synthetic tests must remain self-contained when the repository is
made public.

## Container

Build locally with `docker build -t flighty-mcp .`. The image has no credentials.
Its health check performs an MCP initialize handshake against the container's
loopback interface and never contacts Flighty's API. CI checks image construction
and CLI startup; the HTTP transport also has an in-process ASGI protocol test.

## Supervised mutation testing

No live mutation should run until a user is present and explicitly authorizes a
specific action on a specific test record. A test should describe the existing
state, intended change and restoration strategy first. Never use account deletion,
bulk deletion, account linking, receipt upload or device registration as smoke tests.
The read-only smoke script does not contain a mutation-test mode.
