"""Preserve incoming client identity without sharing account credentials."""
from __future__ import annotations

from typing import Mapping

# Explicitly enumerate client fields: prefix wildcards also match credentials
# and backend routing/turn state, which must not cross service boundaries.
_FORWARDED_NAMES = frozenset(
    {
        "originator",
        "user-agent",
        "version",
        "session_id",
        "conversation_id",
        "session-id",
        "thread-id",
        "x-client-request-id",
        "x-codex-installation-id",
        "x-codex-window-id",
        "x-codex-beta-features",
        "x-codex-turn-metadata",
        "openai-beta",
        "accept",
    }
)


def client_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Forward real client fields only; absence must not manufacture identity."""
    if not headers:
        return {}
    connection_tokens = {
        token.strip().lower()
        for name, value in headers.items() if name.lower() == "connection"
        for token in value.split(",")
    }
    picked: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered not in _FORWARDED_NAMES or lowered in connection_tokens:
            continue
        # An empty feature list is valid protocol metadata; empty identity is not.
        if not value.strip() and not (lowered == "x-codex-beta-features" and value == ""):
            continue
        picked[lowered] = value
    return picked
