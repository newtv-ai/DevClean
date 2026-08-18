r"""Audited Visual Studio Installer package-cache semantics for Windows cleanup.

The Visual Studio Installer package cache is not a generic download directory.
It contains package manifests and, depending on installer policy, retained
payloads used for later modify/repair operations. DevClean inventories the
source-audited default cache without granting raw deletion authority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
class VisualStudioInstallerRootSet:
    package_cache_roots: tuple[PureWindowsPath, ...]
    instance_metadata_roots: tuple[PureWindowsPath, ...]


_VISUAL_STUDIO_INSTALLER_CACHE_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-installer-package-cache-mixed",
    app_id="visual_studio_installer",
    root_key="VISUAL_STUDIO_INSTALLER_CACHE",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Visual Studio Installer package cache; payload retention is installer policy",
)
_VISUAL_STUDIO_INSTALLER_INSTANCE_METADATA_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-installer-instance-metadata",
    app_id="visual_studio_installer",
    root_key="VISUAL_STUDIO_INSTALLER_INSTANCE_METADATA",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Visual Studio Installer instance metadata required for servicing state",
)

VISUAL_STUDIO_INSTALLER_RULES: tuple[ApplicationCleanupRule, ...] = (
    _VISUAL_STUDIO_INSTALLER_CACHE_RULE,
    _VISUAL_STUDIO_INSTALLER_INSTANCE_METADATA_RULE,
)


def visual_studio_installer_roots(
    environment: Mapping[str, str] | None = None,
) -> VisualStudioInstallerRootSet:
    env = _casefold_env(environment)
    program_data = env.get("programdata")
    if not program_data:
        return VisualStudioInstallerRootSet((), ())

    cache = PureWindowsPath(program_data) / "Microsoft" / "VisualStudio" / "Packages"
    metadata = cache / "_Instances"
    return VisualStudioInstallerRootSet((cache,), (metadata,))


def visual_studio_installer_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return visual_studio_installer_roots(environment).package_cache_roots


def match_visual_studio_installer_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = visual_studio_installer_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    for candidates, rule in (
        (
            roots.instance_metadata_roots,
            _VISUAL_STUDIO_INSTALLER_INSTANCE_METADATA_RULE,
        ),
        (roots.package_cache_roots, _VISUAL_STUDIO_INSTALLER_CACHE_RULE),
    ):
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
                matches.append((len(normalized_root), rule))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_visual_studio_installer_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_visual_studio_installer_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = (
        None
        if observed is None
        else max(0.0, (current - observed).total_seconds() / 86_400)
    )
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def visual_studio_installer_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_visual_studio_installer_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def visual_studio_installer_process_running() -> bool:
    """No mutation path is exposed, so no process gate is required yet."""

    return False


def clear_visual_studio_installer_process_cache() -> None:
    return None


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "VISUAL_STUDIO_INSTALLER_RULES",
    "VisualStudioInstallerRootSet",
    "clear_visual_studio_installer_process_cache",
    "evaluate_visual_studio_installer_path",
    "match_visual_studio_installer_rule",
    "visual_studio_installer_audited_tool_roots",
    "visual_studio_installer_process_running",
    "visual_studio_installer_roots",
    "visual_studio_installer_scan_roots",
    "whole_tree_visual_studio_installer_rule",
]
