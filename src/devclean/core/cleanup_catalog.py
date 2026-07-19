"""Known local inventory roots and their non-executable review policy.

The catalog is presentation evidence, never deletion authority.  A path being
inside one of these roots may improve classification, but it cannot create an
executable action or a default selection.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.platform.windows.volumes import is_local_fixed_path


class CleanupCategory(StrEnum):
    USER_TEMP = "USER_TEMP"
    CRASH_DUMPS = "CRASH_DUMPS"
    PIP_CACHE = "PIP_CACHE"
    UV_CACHE = "UV_CACHE"
    NPM_CACHE = "NPM_CACHE"
    PNPM_STORE = "PNPM_STORE"
    CONDA_CACHE = "CONDA_CACHE"
    HUGGINGFACE_CACHE = "HUGGINGFACE_CACHE"
    GRADLE_CACHE = "GRADLE_CACHE"
    YARN_CACHE = "YARN_CACHE"
    OLLAMA_MODELS = "OLLAMA_MODELS"
    VSCODE_CACHE = "VSCODE_CACHE"
    BROWSER_CACHE = "BROWSER_CACHE"
    THUMBNAIL_CACHE = "THUMBNAIL_CACHE"
    CONTAINER_STORAGE = "CONTAINER_STORAGE"
    IDE_CACHE = "IDE_CACHE"
    PROJECT_BUILD_OUTPUT = "PROJECT_BUILD_OUTPUT"
    WINDOWS_UPDATE = "WINDOWS_UPDATE"
    SYSTEM_LOGS = "SYSTEM_LOGS"
    INSTALLERS_DOWNLOADS = "INSTALLERS_DOWNLOADS"
    OTHER = "OTHER"


class CleanupPolicy(StrEnum):
    """How a known root should be presented after a read-only scan."""

    AGE_BASED_REVIEW = "AGE_BASED_REVIEW"
    VENDOR_MANAGED = "VENDOR_MANAGED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"


class SourceDomain(StrEnum):
    """Stable top-level result groups, independent from actionability."""

    AI_MODELS = "AI_MODELS"
    PACKAGE_MANAGERS = "PACKAGE_MANAGERS"
    CONTAINERS_VIRTUALIZATION = "CONTAINERS_VIRTUALIZATION"
    IDE_EDITORS = "IDE_EDITORS"
    PROJECT_BUILD = "PROJECT_BUILD"
    WINDOWS_SYSTEM = "WINDOWS_SYSTEM"
    APPLICATION_CACHE = "APPLICATION_CACHE"
    LOGS_DUMPS_TEMP = "LOGS_DUMPS_TEMP"
    INSTALLERS_DOWNLOADS = "INSTALLERS_DOWNLOADS"
    GENERAL_STORAGE = "GENERAL_STORAGE"


@dataclass(frozen=True, slots=True)
class KnownCleanupRoot:
    path: Path
    category: CleanupCategory
    policy: CleanupPolicy
    label: str


def discover_known_cleanup_roots(
    environment: dict[str, str] | None = None,
    *,
    home: Path | None = None,
    temp_root: Path | None = None,
) -> tuple[KnownCleanupRoot, ...]:
    """Return existing conventional cache roots that are safe to scan locally.

    This function discovers directories only.  It does not launch package
    managers, Docker, Ollama, VS Code, or any cleanup command.
    """

    env = dict(os.environ if environment is None else environment)
    user_home = home or Path.home()
    local = _env_path(env, "LOCALAPPDATA")
    roaming = _env_path(env, "APPDATA")
    pip_cache = _env_path(env, "PIP_CACHE_DIR")
    uv_cache = _env_path(env, "UV_CACHE_DIR")
    npm_cache = _env_path(env, "NPM_CONFIG_CACHE")
    pnpm_home = _env_path(env, "PNPM_HOME")
    huggingface_root = _huggingface_root(env, user_home)
    active_temp = temp_root or Path(tempfile.gettempdir())
    candidates: list[KnownCleanupRoot] = [
        KnownCleanupRoot(
            active_temp,
            CleanupCategory.USER_TEMP,
            CleanupPolicy.AGE_BASED_REVIEW,
            "当前用户临时文件",
        ),
        KnownCleanupRoot(
            local / "CrashDumps" if local else Path(),
            CleanupCategory.CRASH_DUMPS,
            CleanupPolicy.AGE_BASED_REVIEW,
            "用户崩溃转储",
        ),
        KnownCleanupRoot(
            pip_cache or Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "pip 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "pip" / "Cache" if local else Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "pip 缓存",
        ),
        KnownCleanupRoot(
            roaming / "pip" / "Cache" if roaming else Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "pip 缓存",
        ),
        KnownCleanupRoot(
            uv_cache or Path(),
            CleanupCategory.UV_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "uv 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "uv" / "cache" if local else Path(),
            CleanupCategory.UV_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "uv 缓存",
        ),
        KnownCleanupRoot(
            npm_cache or Path(),
            CleanupCategory.NPM_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "npm 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "npm-cache" if local else Path(),
            CleanupCategory.NPM_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "npm 缓存",
        ),
        KnownCleanupRoot(
            pnpm_home / "store" if pnpm_home else Path(),
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.VENDOR_MANAGED,
            "pnpm store (环境变量)",
        ),
        KnownCleanupRoot(
            local / "pnpm" / "store" if local else Path(),
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.VENDOR_MANAGED,
            "pnpm store",
        ),
        KnownCleanupRoot(
            user_home / ".conda" / "pkgs",
            CleanupCategory.CONDA_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Conda 包缓存",
        ),
        KnownCleanupRoot(
            user_home / "miniconda3" / "pkgs",
            CleanupCategory.CONDA_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Miniconda 包缓存",
        ),
        KnownCleanupRoot(
            user_home / "anaconda3" / "pkgs",
            CleanupCategory.CONDA_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Anaconda 包缓存",
        ),
        KnownCleanupRoot(
            huggingface_root,
            CleanupCategory.HUGGINGFACE_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Hugging Face 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".cache" / "huggingface",
            CleanupCategory.HUGGINGFACE_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Hugging Face 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".gradle" / "caches",
            CleanupCategory.GRADLE_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Gradle 缓存",
        ),
        KnownCleanupRoot(
            local / "Yarn" / "Cache" if local else Path(),
            CleanupCategory.YARN_CACHE,
            CleanupPolicy.VENDOR_MANAGED,
            "Yarn 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".ollama" / "models",
            CleanupCategory.OLLAMA_MODELS,
            CleanupPolicy.VENDOR_MANAGED,
            "Ollama 模型存储",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "Cache" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.MANUAL_REVIEW,
            "VS Code Cache",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "CachedData" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.MANUAL_REVIEW,
            "VS Code CachedData",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "GPUCache" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.MANUAL_REVIEW,
            "VS Code GPUCache",
        ),
        KnownCleanupRoot(
            local / "Microsoft" / "Windows" / "Explorer" if local else Path(),
            CleanupCategory.THUMBNAIL_CACHE,
            CleanupPolicy.MANUAL_REVIEW,
            "Windows 缩略图与图标缓存",
        ),
    ]
    candidates.extend(_browser_cache_roots(local, roaming))
    accepted: list[KnownCleanupRoot] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.path.is_absolute() or not candidate.path.is_dir():
            continue
        if not is_local_fixed_path(candidate.path):
            continue
        key = os.path.normcase(os.path.normpath(str(candidate.path)))
        if key not in seen:
            seen.add(key)
            accepted.append(candidate)
    return tuple(sorted(accepted, key=lambda item: (len(item.path.parts), str(item.path))))


def known_root_for_path(
    path: Path, roots: tuple[KnownCleanupRoot, ...]
) -> KnownCleanupRoot | None:
    """Return the most-specific configured root containing *path*."""

    matches = [root for root in roots if _is_descendant(path, root.path)]
    return max(matches, key=lambda root: len(root.path.parts), default=None)


def _env_path(environment: dict[str, str], name: str) -> Path | None:
    value = environment.get(name)
    return Path(value) if value else None


def _huggingface_root(environment: dict[str, str], home: Path) -> Path:
    if root := _env_path(environment, "HF_HUB_CACHE"):
        return root.parent if root.name.casefold() == "hub" else root
    if root := _env_path(environment, "HF_HOME"):
        return root
    return home / ".cache" / "huggingface"


def _browser_cache_roots(
    local: Path | None, roaming: Path | None
) -> tuple[KnownCleanupRoot, ...]:
    """Discover only browser cache subdirectories, never profile data roots."""

    profile_bases = (
        local / "Google" / "Chrome" / "User Data" if local else Path(),
        local / "Microsoft" / "Edge" / "User Data" if local else Path(),
        local / "BraveSoftware" / "Brave-Browser" / "User Data" if local else Path(),
    )
    roots: list[KnownCleanupRoot] = []
    for base in profile_bases:
        for profile in _children(base):
            for cache_name in ("Cache", "Code Cache", "GPUCache"):
                roots.append(
                    KnownCleanupRoot(
                        profile / cache_name,
                        CleanupCategory.BROWSER_CACHE,
                        CleanupPolicy.MANUAL_REVIEW,
                        f"浏览器 {cache_name}",
                    )
                )
    firefox_profiles = roaming / "Mozilla" / "Firefox" / "Profiles" if roaming else Path()
    for profile in _children(firefox_profiles):
        for cache_name in ("cache2", "startupCache"):
            roots.append(
                KnownCleanupRoot(
                    profile / cache_name,
                    CleanupCategory.BROWSER_CACHE,
                    CleanupPolicy.MANUAL_REVIEW,
                    f"Firefox {cache_name}",
                )
            )
    return tuple(roots)


def _children(path: Path) -> tuple[Path, ...]:
    if not path.is_dir() or not is_local_fixed_path(path):
        return ()
    try:
        return tuple(child for child in path.iterdir() if child.is_dir())
    except OSError:
        return ()


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root)))
        ) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def source_domain_for_category(category: CleanupCategory) -> SourceDomain:
    """Map a detailed category to a stable source domain.

    This mapping is deliberately separate from cleanup policy so UI grouping
    cannot be mistaken for an execution decision.
    """

    if category in {CleanupCategory.HUGGINGFACE_CACHE, CleanupCategory.OLLAMA_MODELS}:
        return SourceDomain.AI_MODELS
    if category in {
        CleanupCategory.PIP_CACHE,
        CleanupCategory.UV_CACHE,
        CleanupCategory.NPM_CACHE,
        CleanupCategory.PNPM_STORE,
        CleanupCategory.CONDA_CACHE,
        CleanupCategory.GRADLE_CACHE,
        CleanupCategory.YARN_CACHE,
    }:
        return SourceDomain.PACKAGE_MANAGERS
    if category is CleanupCategory.VSCODE_CACHE:
        return SourceDomain.IDE_EDITORS
    if category is CleanupCategory.IDE_CACHE:
        return SourceDomain.IDE_EDITORS
    if category is CleanupCategory.CONTAINER_STORAGE:
        return SourceDomain.CONTAINERS_VIRTUALIZATION
    if category is CleanupCategory.PROJECT_BUILD_OUTPUT:
        return SourceDomain.PROJECT_BUILD
    if category in {CleanupCategory.USER_TEMP, CleanupCategory.CRASH_DUMPS}:
        return SourceDomain.LOGS_DUMPS_TEMP
    if category is CleanupCategory.THUMBNAIL_CACHE:
        return SourceDomain.WINDOWS_SYSTEM
    if category is CleanupCategory.WINDOWS_UPDATE:
        return SourceDomain.WINDOWS_SYSTEM
    if category is CleanupCategory.SYSTEM_LOGS:
        return SourceDomain.LOGS_DUMPS_TEMP
    if category is CleanupCategory.INSTALLERS_DOWNLOADS:
        return SourceDomain.INSTALLERS_DOWNLOADS
    if category is CleanupCategory.BROWSER_CACHE:
        return SourceDomain.APPLICATION_CACHE
    return SourceDomain.GENERAL_STORAGE


__all__ = [
    "CleanupCategory",
    "CleanupPolicy",
    "KnownCleanupRoot",
    "SourceDomain",
    "discover_known_cleanup_roots",
    "known_root_for_path",
    "source_domain_for_category",
]
