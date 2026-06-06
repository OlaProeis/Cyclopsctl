"""Named configuration profiles from cyclopsctl.toml."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cyclopsctl.config import ConfigError

PROFILE_SECTION = "profile"
ROUTING_PROFILE_SECTION = "routing_profile"

ROUTING_TOP_LEVEL_KEYS = frozenset({"composer_tier", "opus_enabled", "rules_file"})


class ProfileError(ConfigError):
    """Invalid or unknown profile configuration."""


def list_profile_names(raw_config: Mapping[str, Any]) -> list[str]:
    """Return sorted profile names defined in raw TOML."""
    profiles = raw_config.get(PROFILE_SECTION)
    if not isinstance(profiles, dict):
        return []
    return sorted(str(name) for name in profiles)


def list_routing_profile_names(raw_config: Mapping[str, Any]) -> list[str]:
    """Return sorted routing-profile preset names defined in raw TOML."""
    presets = raw_config.get(ROUTING_PROFILE_SECTION)
    if not isinstance(presets, dict):
        return []
    return sorted(str(name) for name in presets)


def _require_table(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be a TOML table")
    return dict(value)


def load_profile(raw_config: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Load a named profile table; raise when missing."""
    profiles = raw_config.get(PROFILE_SECTION)
    if not isinstance(profiles, dict):
        raise ProfileError(f"Unknown profile: {name!r}")
    profile = profiles.get(name)
    if profile is None:
        available = ", ".join(list_profile_names(raw_config)) or "(none defined)"
        raise ProfileError(
            f"Unknown profile: {name!r}. Available profiles: {available}"
        )
    return _require_table(profile, label=f"profile.{name}")


def load_routing_profile(raw_config: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Load a named routing-profile preset; raise when missing."""
    presets = raw_config.get(ROUTING_PROFILE_SECTION)
    if not isinstance(presets, dict):
        raise ProfileError(f"Unknown routing profile: {name!r}")
    preset = presets.get(name)
    if preset is None:
        available = ", ".join(list_routing_profile_names(raw_config)) or "(none defined)"
        raise ProfileError(
            f"Unknown routing profile: {name!r}. "
            f"Available routing profiles: {available}"
        )
    return _require_table(preset, label=f"routing_profile.{name}")


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins)."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _strip_meta_sections(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """Remove profile and routing-profile tables from the top-level config."""
    stripped: dict[str, Any] = {}
    for key, value in raw_config.items():
        if key in {PROFILE_SECTION, ROUTING_PROFILE_SECTION}:
            continue
        stripped[key] = value
    return stripped


def _split_profile_routing(profile_cfg: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Separate run-level profile keys from routing overrides."""
    profile_values = dict(profile_cfg)
    routing_inline = profile_values.pop("routing", {})
    if not isinstance(routing_inline, dict):
        raise ProfileError("profile.routing must be a TOML table")

    routing_profile_name = profile_values.pop("routing_profile", None)
    if routing_profile_name is not None and not str(routing_profile_name).strip():
        raise ProfileError("profile.routing_profile must be a non-empty string")
    if routing_profile_name is not None:
        routing_profile_name = str(routing_profile_name).strip()

    for key in list(profile_values.keys()):
        if key in ROUTING_TOP_LEVEL_KEYS:
            routing_inline[key] = profile_values.pop(key)

    return profile_values, routing_inline, routing_profile_name


def _compose_routing_section(
    *,
    base_routing: Mapping[str, Any] | None,
    routing_profile: Mapping[str, Any] | None,
    profile_routing: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge base, preset, and profile routing tables."""
    routing: dict[str, Any] = {}
    if base_routing:
        routing = _deep_merge(routing, base_routing)
    if routing_profile:
        routing = _deep_merge(routing, routing_profile)
    if profile_routing:
        routing = _deep_merge(routing, profile_routing)
    return routing or None


def merge_effective_file_config(
    raw_config: Mapping[str, Any],
    profile_name: str | None = None,
) -> dict[str, Any]:
    """
    Merge base TOML settings with an optional named profile.

    Precedence within file layers: profile values override base values.
    Routing is composed from base ``[routing]``, optional ``routing_profile``
    preset, and profile-level routing overrides (including ``composer_tier``,
    ``opus_enabled``, and nested ``[profile.<name>.routing]``).
    """
    base = _strip_meta_sections(raw_config)
    if profile_name is None:
        return base

    profile_cfg = load_profile(raw_config, profile_name)
    profile_values, profile_routing, routing_profile_name = _split_profile_routing(
        profile_cfg
    )

    merged = _deep_merge(base, profile_values)

    base_routing = merged.pop("routing", None)
    if base_routing is not None and not isinstance(base_routing, dict):
        raise ProfileError("routing section must be a TOML table")

    routing_preset = (
        load_routing_profile(raw_config, routing_profile_name)
        if routing_profile_name is not None
        else None
    )

    routing = _compose_routing_section(
        base_routing=base_routing if isinstance(base_routing, dict) else None,
        routing_profile=routing_preset,
        profile_routing=profile_routing or None,
    )
    if routing is not None:
        merged["routing"] = routing

    return merged
