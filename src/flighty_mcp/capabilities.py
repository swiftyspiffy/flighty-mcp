"""Flighty 4.12 capability negotiation for the contracts this client supports."""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Any

from . import proto


def client_capabilities() -> dict[str, Any]:
    """Return a fresh payload for both HTTP negotiation and device updates."""
    return {
        "generations": {"AIRPORT": 1, "WHEN_TO_LEAVE_PLAN": 1},
        "features": ["NON_ACTUAL_GATES", "FLIGHT_CONTENT_IDS_ON_SYNC", "WHEN_TO_LEAVE"],
    }


@lru_cache(maxsize=1)
def capability_header() -> str:
    """Match the app: binary protobuf, base64url alphabet, no padding."""
    wire = proto.encode("ClientCapabilitiesProto", client_capabilities())
    return base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")
