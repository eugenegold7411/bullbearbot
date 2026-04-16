"""
feature_flags.py — Single canonical module for reading feature flags (T0.6).

All code that needs to check a flag imports from here.
Never reads strategy_config.json directly from multiple places.

Non-fatal everywhere: returns {} or default on any error.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FLAG_CACHE: dict = {}
_CACHE_LOADED: bool = False
_CONFIG_PATH = Path("strategy_config.json")


def load_flags(force_reload: bool = False) -> dict:
    """
    Read strategy_config.json, merge feature_flags + shadow_flags + lab_flags
    into one flat dict. Cache at module level.
    Returns {} on any error (non-fatal).
    """
    global _FLAG_CACHE, _CACHE_LOADED

    if _CACHE_LOADED and not force_reload:
        return _FLAG_CACHE

    try:
        config = json.loads(_CONFIG_PATH.read_text())
        merged: dict = {}
        merged.update(config.get("feature_flags", {}))
        merged.update(config.get("shadow_flags", {}))
        merged.update(config.get("lab_flags", {}))
        _FLAG_CACHE = merged
        _CACHE_LOADED = True
    except Exception as exc:  # noqa: BLE001
        log.warning("[FLAGS] load_flags failed: %s", exc)
        _FLAG_CACHE = {}
        _CACHE_LOADED = True

    return _FLAG_CACHE


def is_enabled(flag_name: str, default: bool = False) -> bool:
    """
    Return True if flag_name is in loaded flags and its value is True.
    Returns default if flag_name not found.
    Never raises.
    """
    try:
        flags = load_flags()
        if flag_name not in flags:
            return default
        return bool(flags[flag_name])
    except Exception as exc:  # noqa: BLE001
        log.warning("[FLAGS] is_enabled(%s) failed: %s", flag_name, exc)
        return default


def get_all_flags() -> dict:
    """Return a copy of the full merged flag dict. Useful for logging/debugging."""
    try:
        return dict(load_flags())
    except Exception:  # noqa: BLE001
        return {}
