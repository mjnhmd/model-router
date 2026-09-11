from __future__ import annotations

import os
import re

import httpx

RELEASES_URL = os.getenv(
    "MODEL_ROUTER_RELEASES_URL",
    "https://api.github.com/repos/mjnhmd/model-router/releases?per_page=1",
)


def normalize_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|[^0-9])v?(\d+)\.(\d+)\.(\d+)(?:$|[^0-9])", str(value))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


async def check_for_update(current_version: str) -> dict:
    result = {
        "current_version": str(current_version),
        "latest_version": str(current_version),
        "update_available": False,
        "release_url": "",
        "release_name": "",
        "error": "",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.get(RELEASES_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            releases = response.json()
        release = releases[0] if isinstance(releases, list) and releases else {}
        tag = str(release.get("tag_name", "")) if isinstance(release, dict) else ""
        if not tag:
            return result
        latest = normalize_version(tag)
        current = normalize_version(current_version)
        result.update({
            "latest_version": tag.lstrip("v"),
            "update_available": latest > current,
            "release_url": str(release.get("web_url", "") or release.get("_links", {}).get("self", "")),
            "release_name": str(release.get("name", "")),
        })
        return result
    except Exception:  # noqa: BLE001
        result["error"] = "检查更新失败，请稍后重试"
        return result
