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


@dataclass(frozen=True, slots=True)
class UninstallEntry:
    """One program from Windows' installed-programs list (Apps & features)."""

    display_name: str
    install_location: str
    uninstall_string: str
    display_icon: str


_UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def uninstall_entries() -> tuple[UninstallEntry, ...]:
    """Read every registered installed program; unreadable keys are skipped."""

    views = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    )
    entries: list[UninstallEntry] = []
    for hive, view in views:
        try:
            parent = winreg.OpenKey(hive, _UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                try:
                    key = winreg.OpenKey(parent, name, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                try:
                    entries.append(
                        UninstallEntry(
                            display_name=_string_value(key, "DisplayName") or name,
                            install_location=_string_value(key, "InstallLocation"),
                            uninstall_string=_string_value(key, "UninstallString"),
                            display_icon=_string_value(key, "DisplayIcon"),
                        )
                    )
                finally:
                    winreg.CloseKey(key)
        finally:
            winreg.CloseKey(parent)
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class ServiceEntry:
    """One registered Windows service and the strings it is configured with."""

    name: str
    image_path: str
    settings: tuple[str, ...]


_SERVICES_KEY = r"SYSTEM\CurrentControlSet\Services"


def service_entries() -> tuple[ServiceEntry, ...]:
    """Read every registered service: its command, environment and parameters.

    Raises ``OSError`` when the service list itself cannot be opened; a single
    service key that cannot be opened is skipped.
    """

    parent = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICES_KEY, 0, winreg.KEY_READ)
    entries: list[ServiceEntry] = []
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(parent, index)
            except OSError:
                break
            index += 1
            try:
                key = winreg.OpenKey(parent, name, 0, winreg.KEY_READ)
            except OSError:
                continue
            try:
                settings = [
                    value
                    for value_name, value in _text_values(key)
                    if value_name.casefold() == "environment"
                ]
                try:
                    parameters = winreg.OpenKey(key, "Parameters", 0, winreg.KEY_READ)
                except OSError:
                    pass
                else:
                    try:
                        settings.extend(value for _name, value in _text_values(parameters))
                    finally:
                        winreg.CloseKey(parameters)
                entries.append(ServiceEntry(name, _string_value(key, "ImagePath"), tuple(settings)))
            finally:
                winreg.CloseKey(key)
    finally:
        winreg.CloseKey(parent)
    return tuple(entries)


def _text_values(key: winreg.HKEYType) -> list[tuple[str, str]]:
    """Every string value of *key*; a multi-string contributes each of its lines."""

    values: list[tuple[str, str]] = []
    index = 0
    while True:
        try:
            name, data, kind = winreg.EnumValue(key, index)
        except OSError:
            break
        index += 1
        if kind == winreg.REG_SZ and isinstance(data, str):
            values.append((name, data))
        elif kind == winreg.REG_EXPAND_SZ and isinstance(data, str):
            values.append((name, winreg.ExpandEnvironmentStrings(data)))
        elif kind == winreg.REG_MULTI_SZ and isinstance(data, list):
            values.extend((name, str(line)) for line in data)
    return values


def _string_value(key: winreg.HKEYType, name: str) -> str:
    try:
        value, value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    if not isinstance(value, str):
        return ""
    if value_type == winreg.REG_EXPAND_SZ:
        return winreg.ExpandEnvironmentStrings(value)
    return value if value_type == winreg.REG_SZ else ""


__all__ = [
    "RegistryStringLookup",
    "RegistryStringValue",
    "ServiceEntry",
    "UninstallEntry",
    "first_hklm_string_value",
    "query_hklm_string_value",
    "service_entries",
    "uninstall_entries",
]
