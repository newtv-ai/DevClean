r"""Audited Cargo global-cache semantics for Windows cleanup.

Cargo's home directory mixes regenerable registry/git dependency caches with
installed binaries, credentials, configuration, and install metadata. Current
Cargo also performs automatic age-based garbage collection for its global cache,
while manual cache GC remains unstable. DevClean therefore inventories the exact
registry and git cache roots but grants no generic deletion authority.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
)


@dataclass(frozen=True, slots=True)
class CargoRootSet:
    home_roots: tuple[PureWindowsPath, ...]
    registry_roots: tuple[PureWindowsPath, ...]
    git_roots: tuple[PureWindowsPath, ...]
    bin_roots: tuple[PureWindowsPath, ...]
    config_paths: tuple[PureWindowsPath, ...]
    credential_paths: tuple[PureWindowsPath, ...]
    install_metadata_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    rebuild_cost: RebuildCost,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="cargo",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_CARGO_REGISTRY_RULE = _rule(
    "cargo-registry-cache-vendor-managed",
    root_key="CARGO_REGISTRY",
    label="Cargo registry dependency cache; Cargo auto-GC manages unused entries",
    rebuild_cost=RebuildCost.HIGH,
)
_CARGO_GIT_RULE = _rule(
    "cargo-git-cache-vendor-managed",
    root_key="CARGO_GIT",
    label="Cargo git dependency cache; Cargo auto-GC manages unused entries",
    rebuild_cost=RebuildCost.HIGH,
)
_CARGO_HOME_RULE = _rule(
    "cargo-home-state",
    root_key="CARGO_HOME",
    label="Cargo home persistent state and unknown storage",
    rebuild_cost=RebuildCost.HIGH,
)
_CARGO_BIN_RULE = _rule(
    "cargo-installed-binaries",
    root_key="CARGO_BIN",
    label="Cargo/rustup installed command binaries",
    rebuild_cost=RebuildCost.HIGH,
)
_CARGO_CONFIG_RULE = _rule(
    "cargo-configuration",
    root_key="CARGO_CONFIG",
    label="Cargo configuration",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)
_CARGO_CREDENTIAL_RULE = _rule(
    "cargo-credentials",
    root_key="CARGO_CREDENTIALS",
    label="Cargo registry credentials",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)
_CARGO_INSTALL_METADATA_RULE = _rule(
    "cargo-install-metadata",
    root_key="CARGO_INSTALL_METADATA",
    label="Cargo installed-crate metadata",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)
_CARGO_PROJECT_METADATA_RULE = _rule(
    "cargo-project-metadata",
    root_key="ANYWHERE",
    label="Rust/Cargo project dependency metadata",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)
_CARGO_PROJECT_CONFIG_RULE = _rule(
    "cargo-project-configuration",
    root_key="ANYWHERE",
    label="Project-local Cargo configuration",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)

CARGO_RULES: tuple[ApplicationCleanupRule, ...] = (
    _CARGO_REGISTRY_RULE,
    _CARGO_GIT_RULE,
    _CARGO_HOME_RULE,
    _CARGO_BIN_RULE,
    _CARGO_CONFIG_RULE,
    _CARGO_CREDENTIAL_RULE,
    _CARGO_INSTALL_METADATA_RULE,
    _CARGO_PROJECT_METADATA_RULE,
    _CARGO_PROJECT_CONFIG_RULE,
)

_PROJECT_METADATA_NAMES = frozenset({"cargo.toml", "cargo.lock"})
_PROJECT_CONFIG_NAMES = frozenset({"config", "config.toml"})


def cargo_roots(environment: Mapping[str, str] | None = None) -> CargoRootSet:
    env = _casefold_env(environment)
    home = env.get("devclean_cargo_home") or env.get("cargo_home")
    if not home:
        userprofile = env.get("userprofile")
        if userprofile:
            home = str(PureWindowsPath(userprofile) / ".cargo")

    cargo_home = _absolute_path(home)
    if cargo_home is None:
        return CargoRootSet((), (), (), (), (), (), ())

    return CargoRootSet(
        home_roots=(cargo_home,),
        registry_roots=(cargo_home / "registry",),
        git_roots=(cargo_home / "git",),
        bin_roots=(cargo_home / "bin",),
        config_paths=(cargo_home / "config.toml", cargo_home / "config"),
        credential_paths=(
            cargo_home / "credentials.toml",
            cargo_home / "credentials",
        ),
        install_metadata_paths=(
            cargo_home / ".crates.toml",
            cargo_home / ".crates2.json",
        ),
    )


def cargo_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = cargo_roots(environment)
    return tuple(dict.fromkeys((*roots.registry_roots, *roots.git_roots)))


def match_cargo_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = cargo_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []

    groups = (
        (roots.registry_roots, _CARGO_REGISTRY_RULE, MatchKind.PREFIX),
        (roots.git_roots, _CARGO_GIT_RULE, MatchKind.PREFIX),
        (roots.bin_roots, _CARGO_BIN_RULE, MatchKind.PREFIX),
        (roots.config_paths, _CARGO_CONFIG_RULE, MatchKind.EXACT),
        (roots.credential_paths, _CARGO_CREDENTIAL_RULE, MatchKind.EXACT),
        (roots.install_metadata_paths, _CARGO_INSTALL_METADATA_RULE, MatchKind.EXACT),
        (roots.home_roots, _CARGO_HOME_RULE, MatchKind.PREFIX),
    )
    for candidates, rule, match_kind in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, match_kind):
                matches.append((len(normalized_root), rule))

    windows_path = PureWindowsPath(str(path))
    name = windows_path.name.casefold()
    if name in _PROJECT_METADATA_NAMES:
        matches.append((len(normalized), _CARGO_PROJECT_METADATA_RULE))
    parent = windows_path.parent.name.casefold()
    if parent == ".cargo" and name in _PROJECT_CONFIG_NAMES:
        matches.append((len(normalized), _CARGO_PROJECT_CONFIG_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_cargo_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_cargo_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def cargo_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_cargo_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def cargo_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:cargo|rustc|rustup|rust-analyzer)\\.exe$' "
        "}; if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def clear_cargo_process_cache() -> None:
    cargo_process_running.cache_clear()


def _absolute_path(value: str | None) -> PureWindowsPath | None:
    if not value:
        return None
    candidate = PureWindowsPath(value)
    return candidate if candidate.is_absolute() else None


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "CARGO_RULES",
    "CargoRootSet",
    "cargo_audited_tool_roots",
    "cargo_process_running",
    "cargo_roots",
    "cargo_scan_roots",
    "clear_cargo_process_cache",
    "evaluate_cargo_path",
    "match_cargo_rule",
    "whole_tree_cargo_rule",
]
