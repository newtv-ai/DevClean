"""Discover configured cleanup roots without carrying a second catalog in code."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from devclean.core.rule_schema import CleanupCategory, CleanupPolicy, SourceDomain
from devclean.core.user_rules import (
    RuleConfigError,
    ScanRules,
    expand_braces,
    expand_path,
)
from devclean.platform.windows.volumes import is_local_fixed_path


@dataclass(frozen=True, slots=True)
class KnownCleanupRoot:
    path: Path
    category: CleanupCategory
    policy: CleanupPolicy
    label: str
    allow_inside_system_anchor: bool = False


def discover_known_cleanup_roots(
    rules: ScanRules,
    environment: dict[str, str] | None = None,
    *,
    home: Path | None = None,
    temp_root: Path | None = None,
) -> tuple[KnownCleanupRoot, ...]:
    """Resolve the enabled entries in ``scan-rules.json``.

    The implementation knows how to expand paths and filter unsafe storage; the
    actual locations, categories, policies and labels all live in the JSON file.
    """

    env = dict(os.environ if environment is None else environment)
    if home is not None:
        env["USERPROFILE"] = str(home)
    if temp_root is not None:
        env["TEMP"] = str(temp_root)
        env["TMP"] = str(temp_root)

    accepted: list[KnownCleanupRoot] = []
    seen: set[str] = set()
    for configured in rules.known_cleanup_roots:
        if not configured.enabled:
            continue
        try:
            category = CleanupCategory(configured.category)
            policy = CleanupPolicy(configured.policy)
        except ValueError as error:
            raise RuleConfigError(
                f"scan-rules.json 的 {configured.rule_id} 使用了未知 category 或 policy"
            ) from error
        for pattern in configured.patterns:
            for expanded_pattern in expand_braces(expand_path(pattern, env)):
                if "%" in expanded_pattern:
                    continue
                matches = (
                    glob.iglob(expanded_pattern)
                    if glob.has_magic(expanded_pattern)
                    else (expanded_pattern,)
                )
                for match in matches:
                    path = Path(match)
                    try:
                        if not path.is_absolute() or not path.is_dir():
                            continue
                        if not is_local_fixed_path(path):
                            continue
                    except OSError:
                        continue
                    key = os.path.normcase(os.path.normpath(str(path)))
                    if key in seen:
                        continue
                    seen.add(key)
                    accepted.append(
                        KnownCleanupRoot(
                            path=path,
                            category=category,
                            policy=policy,
                            label=configured.label,
                            allow_inside_system_anchor=(
                                configured.allow_inside_system_anchor
                            ),
                        )
                    )
    return tuple(
        sorted(accepted, key=lambda item: (len(item.path.parts), str(item.path)))
    )


def known_root_for_path(
    path: Path, roots: tuple[KnownCleanupRoot, ...]
) -> KnownCleanupRoot | None:
    """Return the most-specific configured root containing *path*."""

    if not roots:
        return None
    target = _normalized(path)
    for prefix, root in _normalized_roots(roots):
        if target == prefix or target.startswith(_with_separator(prefix)):
            return root
    return None


@lru_cache(maxsize=8)
def _normalized_roots(
    roots: tuple[KnownCleanupRoot, ...],
) -> tuple[tuple[str, KnownCleanupRoot], ...]:
    return tuple(
        sorted(
            ((_normalized(root.path), root) for root in roots),
            key=lambda pair: (len(pair[1].path.parts), len(pair[0])),
            reverse=True,
        )
    )


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _with_separator(prefix: str) -> str:
    return prefix if prefix.endswith(os.sep) else prefix + os.sep


def source_domain_for_category(
    category: CleanupCategory, configured: dict[str, str]
) -> SourceDomain:
    """Resolve the UI group from ``delete-rules.json``."""

    try:
        return SourceDomain(configured[category.value])
    except (KeyError, ValueError) as error:
        raise RuleConfigError(
            f"delete-rules.json 缺少 {category.value} 的 source domain"
        ) from error


__all__ = [
    "CleanupCategory",
    "CleanupPolicy",
    "KnownCleanupRoot",
    "SourceDomain",
    "discover_known_cleanup_roots",
    "known_root_for_path",
    "source_domain_for_category",
]
