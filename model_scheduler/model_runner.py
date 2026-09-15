"""Safe Docker argv construction for manifest-managed model containers."""
from __future__ import annotations


def docker_stop_argv(requested_name: str, manifest_name: str) -> list[str] | None:
    if requested_name != manifest_name or not manifest_name.startswith("sms-"):
        return None
    return ["docker", "stop", "--time", "30", manifest_name]
