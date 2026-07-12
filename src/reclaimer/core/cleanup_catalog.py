"""Known local cleanup roots for the GUI without third-party command execution."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reclaimer.platform.windows.volumes import is_local_fixed_path


class CleanupCategory(StrEnum):
    USER_TEMP = "USER_TEMP"
    CRASH_DUMPS = "CRASH_DUMPS"
    PIP_CACHE = "PIP_CACHE"
    UV_CACHE = "UV_CACHE"
    NPM_CACHE = "NPM_CACHE"
    PNPM_STORE = "PNPM_STORE"
    HUGGINGFACE_CACHE = "HUGGINGFACE_CACHE"
    GRADLE_CACHE = "GRADLE_CACHE"
    YARN_CACHE = "YARN_CACHE"
    OLLAMA_MODELS = "OLLAMA_MODELS"
    VSCODE_CACHE = "VSCODE_CACHE"
    BROWSER_CACHE = "BROWSER_CACHE"
    THUMBNAIL_CACHE = "THUMBNAIL_CACHE"
    OTHER = "OTHER"


class CleanupPolicy(StrEnum):
    AUTO_AFTER_AGE = "AUTO_AFTER_AGE"
    AI_REVIEW = "AI_REVIEW"


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
            CleanupPolicy.AUTO_AFTER_AGE,
            "当前用户临时文件",
        ),
        KnownCleanupRoot(
            local / "CrashDumps" if local else Path(),
            CleanupCategory.CRASH_DUMPS,
            CleanupPolicy.AUTO_AFTER_AGE,
            "用户崩溃转储",
        ),
        KnownCleanupRoot(
            pip_cache or Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.AI_REVIEW,
            "pip 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "pip" / "Cache" if local else Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.AI_REVIEW,
            "pip 缓存",
        ),
        KnownCleanupRoot(
            roaming / "pip" / "Cache" if roaming else Path(),
            CleanupCategory.PIP_CACHE,
            CleanupPolicy.AI_REVIEW,
            "pip 缓存",
        ),
        KnownCleanupRoot(
            uv_cache or Path(),
            CleanupCategory.UV_CACHE,
            CleanupPolicy.AI_REVIEW,
            "uv 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "uv" / "cache" if local else Path(),
            CleanupCategory.UV_CACHE,
            CleanupPolicy.AI_REVIEW,
            "uv 缓存",
        ),
        KnownCleanupRoot(
            npm_cache or Path(),
            CleanupCategory.NPM_CACHE,
            CleanupPolicy.AI_REVIEW,
            "npm 缓存 (环境变量)",
        ),
        KnownCleanupRoot(
            local / "npm-cache" if local else Path(),
            CleanupCategory.NPM_CACHE,
            CleanupPolicy.AI_REVIEW,
            "npm 缓存",
        ),
        KnownCleanupRoot(
            pnpm_home / "store" if pnpm_home else Path(),
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.AI_REVIEW,
            "pnpm store (环境变量)",
        ),
        KnownCleanupRoot(
            local / "pnpm" / "store" if local else Path(),
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.AI_REVIEW,
            "pnpm store",
        ),
        KnownCleanupRoot(
            huggingface_root,
            CleanupCategory.HUGGINGFACE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "Hugging Face 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".cache" / "huggingface",
            CleanupCategory.HUGGINGFACE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "Hugging Face 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".gradle" / "caches",
            CleanupCategory.GRADLE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "Gradle 缓存",
        ),
        KnownCleanupRoot(
            local / "Yarn" / "Cache" if local else Path(),
            CleanupCategory.YARN_CACHE,
            CleanupPolicy.AI_REVIEW,
            "Yarn 缓存",
        ),
        KnownCleanupRoot(
            user_home / ".ollama" / "models",
            CleanupCategory.OLLAMA_MODELS,
            CleanupPolicy.AI_REVIEW,
            "Ollama 模型存储",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "Cache" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "VS Code Cache",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "CachedData" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "VS Code CachedData",
        ),
        KnownCleanupRoot(
            roaming / "Code" / "GPUCache" if roaming else Path(),
            CleanupCategory.VSCODE_CACHE,
            CleanupPolicy.AI_REVIEW,
            "VS Code GPUCache",
        ),
        KnownCleanupRoot(
            local / "Microsoft" / "Windows" / "Explorer" if local else Path(),
            CleanupCategory.THUMBNAIL_CACHE,
            CleanupPolicy.AI_REVIEW,
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
                        CleanupPolicy.AI_REVIEW,
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
                    CleanupPolicy.AI_REVIEW,
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


__all__ = [
    "CleanupCategory",
    "CleanupPolicy",
    "KnownCleanupRoot",
    "discover_known_cleanup_roots",
    "known_root_for_path",
]
