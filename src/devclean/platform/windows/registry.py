"""Small fail-closed Windows registry primitives for audited application policy."""

from __future__ import annotations

import winreg
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegistryStringValue:
    value: str
    value_type: int
    key_path: str


@dataclass(frozen=True, slots=True)
class RegistryStringLookup:
    """Result of a registry lookup whose absence may or may not be conclusive."""

    value: RegistryStringValue | None
    conclusive: bool


def query_hklm_string_value(key_path: str, value_name: str) -> RegistryStringLookup:
    """Read one HKLM string value without registry-view ambiguity.

    Missing keys/values are conclusive absence. Access failures, malformed values,
    and unsupported types are inconclusive so callers can fail closed rather than
    silently substituting a lower-priority/default policy.
    """

    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access)
    except FileNotFoundError:
        return RegistryStringLookup(None, True)
    except OSError:
        return RegistryStringLookup(None, False)

    try:
        try:
            raw_value, value_type = winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return RegistryStringLookup(None, True)
        except OSError:
            return RegistryStringLookup(None, False)
    finally:
        winreg.CloseKey(key)

    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
        return RegistryStringLookup(None, False)
    if not isinstance(raw_value, str) or not raw_value:
        return RegistryStringLookup(None, False)
    return RegistryStringLookup(
        RegistryStringValue(raw_value, value_type, key_path),
        True,
    )


def first_hklm_string_value(
    key_paths: Iterable[str],
    value_name: str,
) -> RegistryStringLookup:
    """Return the first discovered HKLM string using caller-defined precedence."""

    for key_path in key_paths:
        lookup = query_hklm_string_value(key_path, value_name)
        if not lookup.conclusive:
            return lookup
        if lookup.value is not None:
            return lookup
    return RegistryStringLookup(None, True)


__all__ = [
    "RegistryStringLookup",
    "RegistryStringValue",
    "first_hklm_string_value",
    "query_hklm_string_value",
]
