"""Discover configured and audited application cleanup roots."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from devclean.core.application_cleanup import (
    CLAUDE_RULES,
    CODEX_RULES,
    CURSOR_RULES,
    ApplicationCleanupRule,
    DecisionOwner,
    application_roots,
    application_scan_roots,
    audited_dynamic_tool_roots,
)
from devclean.core.cargo_cleanup import match_cargo_rule
from devclean.core.conda_cleanup import match_conda_rule
from devclean.core.go_cleanup import match_go_rule
from devclean.core.huggingface_cleanup import match_huggingface_rule
from devclean.core.maven_cleanup import match_maven_rule
from devclean.core.nuget_cleanup import match_nuget_rule
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy, SourceDomain
from devclean.core.user_rules import (
    RuleConfigError,
    ScanRules,
    expand_braces,
    expand_path,
)
from devclean.core.uv_cleanup import match_uv_rule
from devclean.platform.windows.volumes import is_local_fixed_path


@dataclass(frozen=True, slots=True)
class KnownCleanupRoot:
    path: Path
    category: CleanupCategory
    policy: CleanupPolicy
    label: str
    allow_inside_system_anchor: bool = False
    delete_root_itself: bool = False
    application_rule: ApplicationCleanupRule | None = None


def discover_known_cleanup_roots(
    rules: ScanRules,
    environment: dict[str, str] | None = None,
    *,
    home: Path | None = None,
    temp_root: Path | None = None,
) -> tuple[KnownCleanupRoot, ...]:
    """Resolve configured roots plus audited application storage locations.

    Application scan roots are REPORT_ONLY traversal anchors. More-specific TOOL
    rules that explicitly allow whole-tree deletion are added as vendor-managed
    roots. This makes redirected locations discoverable without granting generic
    delete authority to the rest of an application's state directory.
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
                    _append_root(
                        accepted,
                        seen,
                        Path(match),
                        category=category,
                        policy=policy,
                        label=configured.label,
                        allow_inside_system_anchor=configured.allow_inside_system_anchor,
                        delete_root_itself=configured.rule_id in rules.delete_root_ids,
                    )

    _append_application_roots(accepted, seen, env)
    return tuple(
        sorted(accepted, key=lambda item: (len(item.path.parts), str(item.path)))
    )


def _append_application_roots(
    accepted: list[KnownCleanupRoot],
    seen: set[str],
    environment: dict[str, str],
) -> None:
    for root in application_scan_roots(environment):
        category, label = _report_only_root_metadata(root, environment)
        _append_root(
            accepted,
            seen,
            Path(str(root)),
            category=category,
            policy=CleanupPolicy.REPORT_ONLY,
            label=label,
            delete_root_itself=False,
            replace_existing=True,
        )

    root_map = {root.key: root.path for root in application_roots(environment)}
    for rule in (*CODEX_RULES, *CLAUDE_RULES, *CURSOR_RULES):
        if rule.owner is not DecisionOwner.TOOL or not rule.allow_whole_tree:
            continue
        base = root_map.get(rule.root_key)
        if base is None:
            continue
        for relative in expand_braces(rule.relative_pattern):
            if any(token in relative for token in ("*", "?", "[")):
                continue
            path = Path(str(base / relative)) if relative else Path(str(base))
            _append_root(
                accepted,
                seen,
                path,
                category=_application_category(rule.rule_id),
                policy=CleanupPolicy.VENDOR_MANAGED,
                label=rule.label,
                delete_root_itself=True,
                application_rule=rule,
            )

    for dynamic_path, rule in audited_dynamic_tool_roots(environment):
        _append_root(
            accepted,
            seen,
            Path(str(dynamic_path)),
            category=_application_category(rule.rule_id),
            policy=CleanupPolicy.VENDOR_MANAGED,
            label=rule.label,
            delete_root_itself=True,
            application_rule=rule,
        )


def _report_only_root_metadata(
    root: os.PathLike[str],
    environment: dict[str, str],
) -> tuple[CleanupCategory, str]:
    huggingface_rule = match_huggingface_rule(root, environment)
    if huggingface_rule is not None and huggingface_rule.rule_id in {
        "huggingface-hub-cache-vendor-managed",
        "huggingface-xet-cache-vendor-managed",
        "huggingface-assets-cache-vendor-managed",
    }:
        return CleanupCategory.HUGGINGFACE_CACHE, huggingface_rule.label

    maven_rule = match_maven_rule(root, environment)
    if (
        maven_rule is not None
        and maven_rule.rule_id == "maven-local-repository-mixed"
    ):
        return CleanupCategory.MAVEN_REPOSITORY, maven_rule.label

    cargo_rule = match_cargo_rule(root, environment)
    if cargo_rule is not None and cargo_rule.rule_id in {
        "cargo-registry-cache-vendor-managed",
        "cargo-git-cache-vendor-managed",
    }:
        return CleanupCategory.CARGO_REGISTRY, cargo_rule.label

    go_rule = match_go_rule(root, environment)
    if go_rule is not None and go_rule.rule_id in {
        "go-build-cache-vendor-managed",
        "go-module-cache-vendor-managed",
    }:
        return CleanupCategory.GO_MODULE_CACHE, go_rule.label

    nuget_rule = match_nuget_rule(root, environment)
    if nuget_rule is not None and nuget_rule.rule_id.startswith("nuget-"):
        return CleanupCategory.NUGET_CACHE, nuget_rule.label

    conda_rule = match_conda_rule(root, environment)
    if (
        conda_rule is not None
        and conda_rule.rule_id == "conda-package-cache-vendor-managed"
    ):
        return CleanupCategory.CONDA_CACHE, conda_rule.label

    uv_rule = match_uv_rule(root, environment)
    if uv_rule is not None and uv_rule.rule_id == "uv-cache-vendor-managed":
        return CleanupCategory.UV_CACHE, uv_rule.label

    return CleanupCategory.IDE_CACHE, "已审计的应用存储根目录"


def _application_category(rule_id: str) -> CleanupCategory:
    lower = rule_id.casefold()
    if lower in {
        "android-studio-product-logs",
        "gradle-daemon-log",
        "jetbrains-product-logs",
        "toolbox-product-logs",
    }:
        return CleanupCategory.SYSTEM_LOGS
    if lower == "android-sdk-install-temp":
        return CleanupCategory.INSTALLERS_DOWNLOADS
    if lower.startswith("gradle-"):
        return CleanupCategory.GRADLE_CACHE
    if lower == "toolbox-download-cache":
        return CleanupCategory.INSTALLERS_DOWNLOADS
    if lower == "toolbox-install-temp":
        return CleanupCategory.USER_TEMP
    if lower.startswith(("android-studio-", "jetbrains-", "toolbox-")):
        return CleanupCategory.IDE_CACHE
    if "crash" in lower:
        return CleanupCategory.CRASH_DUMPS
    if "log" in lower or "debug" in lower:
        return CleanupCategory.SYSTEM_LOGS
    if lower.startswith("chrome-updater-") or lower.startswith(
        "brave-updater-install"
    ):
        return CleanupCategory.INSTALLERS_DOWNLOADS
    if lower.startswith(
        ("brave-", "chrome-", "edge-", "firefox-", "opera-", "vivaldi-")
    ):
        return CleanupCategory.BROWSER_CACHE
    if lower.startswith("pip-"):
        return CleanupCategory.PIP_CACHE
    if lower.startswith("uv-cache"):
        return CleanupCategory.UV_CACHE
    if lower.startswith("conda-"):
        return CleanupCategory.CONDA_CACHE
    if lower.startswith("nuget-"):
        return CleanupCategory.NUGET_CACHE
    if lower.startswith("go-"):
        return CleanupCategory.GO_MODULE_CACHE
    if lower.startswith("cargo-"):
        return CleanupCategory.CARGO_REGISTRY
    if lower.startswith("maven-"):
        return CleanupCategory.MAVEN_REPOSITORY
    if lower.startswith("huggingface-"):
        return CleanupCategory.HUGGINGFACE_CACHE
    if lower.startswith("yarn-"):
        return CleanupCategory.YARN_CACHE
    if lower.startswith("bun-"):
        return CleanupCategory.BUN_CACHE
    if lower.startswith(("npm-", "pnpm-")):
        return CleanupCategory.NPM_CACHE
    if "temp" in lower or "shell" in lower:
        return CleanupCategory.USER_TEMP
    if "cache" in lower or "plugin" in lower or "extension" in lower:
        return CleanupCategory.IDE_CACHE
    return CleanupCategory.OTHER


def _append_root(
    accepted: list[KnownCleanupRoot],
    seen: set[str],
    path: Path,
    *,
    category: CleanupCategory,
    policy: CleanupPolicy,
    label: str,
    allow_inside_system_anchor: bool = False,
    delete_root_itself: bool = False,
    application_rule: ApplicationCleanupRule | None = None,
    replace_existing: bool = False,
) -> None:
    try:
        if not path.is_absolute() or not path.is_dir():
            return
        if not is_local_fixed_path(path):
            return
    except OSError:
        return
    key = os.path.normcase(os.path.normpath(str(path)))
    replacement = KnownCleanupRoot(
        path=path,
        category=category,
        policy=policy,
        label=label,
        allow_inside_system_anchor=allow_inside_system_anchor,
        delete_root_itself=delete_root_itself,
        application_rule=application_rule,
    )
    if key in seen:
        if replace_existing or delete_root_itself:
            for index, existing in enumerate(accepted):
                existing_key = os.path.normcase(os.path.normpath(str(existing.path)))
                if existing_key == key:
                    accepted[index] = replacement
                    break
        return
    seen.add(key)
    accepted.append(replacement)


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
