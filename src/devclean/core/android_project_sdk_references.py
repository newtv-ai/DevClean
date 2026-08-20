"""Read-only positive Android SDK package references from selected Gradle projects.

This module deliberately does not evaluate Gradle. Gradle scripts are executable
build logic, so static text can provide positive evidence for a few literal SDK
version declarations but absence of a match can never mean "unused".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_MAX_SCRIPT_BYTES = 2 * 1024 * 1024
_MAX_BUILD_FILES = 500
_MAX_DEPTH = 8
_BUILD_FILENAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
    }
)
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".cxx",
        ".externalnativebuild",
        "build",
        "node_modules",
        "out",
    }
)
_VERSION_LITERAL = r"[0-9][0-9A-Za-z._+\-]*"
_COMPILE_SDK_RE = re.compile(
    r"\bcompileSdk(?:Version)?\b\s*(?:=\s*)?"
    r"(?P<value>\d+|['\"]android-\d+['\"])",
)
_BUILD_TOOLS_RE = re.compile(
    rf"\bbuildToolsVersion\b\s*(?:=\s*)?['\"](?P<value>{_VERSION_LITERAL})['\"]"
)
_NDK_RE = re.compile(rf"\bndkVersion\b\s*(?:=\s*)?['\"](?P<value>{_VERSION_LITERAL})['\"]")
_CMAKE_VERSION_RE = re.compile(rf"\bversion\b\s*(?:=\s*)?['\"](?P<value>{_VERSION_LITERAL})['\"]")
_SETTINGS_RELEASE_RE = re.compile(r"\bversion\s*=\s*release\(\s*(?P<value>\d+)\s*\)")


@dataclass(frozen=True, slots=True)
class AndroidProjectSdkReference:
    """One explicit literal project declaration that maps to an SDK package ID."""

    project_root: Path
    source_file: Path
    line_number: int
    kind: str
    raw_value: str
    package_id: str


@dataclass(frozen=True, slots=True)
class AndroidProjectReferenceScan:
    """Read-only result for one exact user-selected Gradle project directory."""

    project_root: Path
    references: tuple[AndroidProjectSdkReference, ...]
    files_scanned: int
    warnings: tuple[str, ...]


def scan_android_project_sdk_references(project_root: Path | str) -> AndroidProjectReferenceScan:
    """Scan literal Android SDK version declarations without executing Gradle.

    The selected root must itself be an ordinary local-fixed directory. Reparse
    descendants are skipped and reported. Generated/cache directories are not
    searched. The result is positive evidence only: a missing package reference
    never proves that the package is unused.
    """

    root = _ordinary_local_directory(Path(project_root), "Android project root")
    direct_names = {
        child.name.casefold()
        for child in root.iterdir()
        if child.is_file() and child.name.casefold() in _BUILD_FILENAMES
    }
    if not direct_names:
        raise ValueError(
            "所选目录没有直接的 settings.gradle(.kts) 或 build.gradle(.kts); "
            "请选择 Gradle/Android 项目根或模块目录"
        )

    references: list[AndroidProjectSdkReference] = []
    warnings: list[str] = []
    files_scanned = 0
    root_parts = len(root.parts)

    for current_text, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_text)
        depth = len(current.parts) - root_parts
        if depth >= _MAX_DEPTH:
            if dirnames:
                warnings.append(f"{current}: 达到静态扫描深度上限 {_MAX_DEPTH}, 更深目录未扫描")
            dirnames[:] = []
        else:
            kept: list[str] = []
            for dirname in dirnames:
                child = current / dirname
                if dirname.casefold() in _EXCLUDED_DIRS:
                    continue
                try:
                    if child.is_symlink() or child.is_junction():
                        warnings.append(f"{child}: reparse/symlink/junction 子目录未扫描")
                        continue
                    metadata = read_file_metadata(child)
                    if metadata.is_reparse_point:
                        warnings.append(f"{child}: reparse 子目录未扫描")
                        continue
                except OSError as error:
                    warnings.append(f"{child}: 无法验证目录, 未扫描 ({error})")
                    continue
                kept.append(dirname)
            dirnames[:] = kept

        for filename in filenames:
            if filename.casefold() not in _BUILD_FILENAMES:
                continue
            script = current / filename
            files_scanned += 1
            if files_scanned > _MAX_BUILD_FILES:
                warnings.append(
                    f"项目 build/settings 文件超过 {_MAX_BUILD_FILES} 个; 其余文件未扫描"
                )
                return _finish_scan(root, references, _MAX_BUILD_FILES, warnings)
            try:
                text = _stable_read_script(script)
            except (OSError, RuntimeError, UnicodeError, ValueError) as error:
                warnings.append(f"{script}: 无法稳定读取 ({error})")
                continue
            references.extend(_parse_script_text(root, script, text))

    return _finish_scan(root, references, files_scanned, warnings)


def _finish_scan(
    root: Path,
    references: list[AndroidProjectSdkReference],
    files_scanned: int,
    warnings: list[str],
) -> AndroidProjectReferenceScan:
    unique: dict[tuple[str, str, int, str], AndroidProjectSdkReference] = {}
    for reference in references:
        key = (
            reference.package_id.casefold(),
            os.path.normcase(os.path.abspath(reference.source_file)),
            reference.line_number,
            reference.kind,
        )
        unique.setdefault(key, reference)
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.package_id.casefold(),
                os.path.normcase(os.path.abspath(item.source_file)),
                item.line_number,
            ),
        )
    )
    return AndroidProjectReferenceScan(
        project_root=root,
        references=ordered,
        files_scanned=files_scanned,
        warnings=tuple(warnings),
    )


def _parse_script_text(
    project_root: Path,
    source_file: Path,
    source: str,
) -> tuple[AndroidProjectSdkReference, ...]:
    """Extract only literal declarations whose package mapping is unambiguous."""

    text = _strip_comments(source)
    found: list[AndroidProjectSdkReference] = []

    for match in _outside_string_matches(_COMPILE_SDK_RE, text):
        raw = match.group("value").strip("'\"")
        api_level = raw.removeprefix("android-")
        found.append(
            _reference(
                project_root,
                source_file,
                text,
                match.start(),
                "compileSdk",
                raw,
                f"platforms;android-{api_level}",
            )
        )

    for block_start, block_text in _named_blocks(text, "compileSdk"):
        for match in _outside_string_matches(_SETTINGS_RELEASE_RE, block_text):
            api_level = match.group("value")
            found.append(
                _reference(
                    project_root,
                    source_file,
                    text,
                    block_start + match.start(),
                    "compileSdk(settings)",
                    api_level,
                    f"platforms;android-{api_level}",
                )
            )

    for match in _outside_string_matches(_BUILD_TOOLS_RE, text):
        version = match.group("value")
        found.append(
            _reference(
                project_root,
                source_file,
                text,
                match.start(),
                "buildToolsVersion",
                version,
                f"build-tools;{version}",
            )
        )

    for match in _outside_string_matches(_NDK_RE, text):
        version = match.group("value")
        found.append(
            _reference(
                project_root,
                source_file,
                text,
                match.start(),
                "ndkVersion",
                version,
                f"ndk;{version}",
            )
        )

    for outer_start, outer in _named_blocks(text, "externalNativeBuild"):
        for cmake_start, cmake_block in _named_blocks(outer, "cmake"):
            for match in _outside_string_matches(_CMAKE_VERSION_RE, cmake_block):
                version = match.group("value")
                found.append(
                    _reference(
                        project_root,
                        source_file,
                        text,
                        outer_start + cmake_start + match.start(),
                        "cmakeVersion",
                        version,
                        f"cmake;{version}",
                    )
                )

    return tuple(found)


def _reference(
    project_root: Path,
    source_file: Path,
    text: str,
    offset: int,
    kind: str,
    raw_value: str,
    package_id: str,
) -> AndroidProjectSdkReference:
    return AndroidProjectSdkReference(
        project_root=project_root,
        source_file=source_file,
        line_number=text.count("\n", 0, max(0, offset)) + 1,
        kind=kind,
        raw_value=raw_value,
        package_id=package_id,
    )


def _outside_string_matches(pattern: re.Pattern[str], text: str) -> tuple[re.Match[str], ...]:
    spans = _string_spans(text)
    return tuple(
        match for match in pattern.finditer(text) if not _offset_in_spans(match.start(), spans)
    )


def _named_blocks(text: str, name: str) -> tuple[tuple[int, str], ...]:
    pattern = re.compile(rf"\b{re.escape(name)}\b\s*\{{")
    spans = _string_spans(text)
    blocks: list[tuple[int, str]] = []
    for match in pattern.finditer(text):
        if _offset_in_spans(match.start(), spans):
            continue
        brace = text.find("{", match.start(), match.end())
        if brace < 0:
            continue
        end = _matching_brace(text, brace)
        if end is None:
            continue
        blocks.append((brace + 1, text[brace + 1 : end]))
    return tuple(blocks)


def _string_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char not in {"'", '"'}:
            index += 1
            continue
        start = index
        triple = text.startswith(char * 3, index)
        index += 3 if triple else 1
        escaped = False
        while index < len(text):
            if not triple and escaped:
                escaped = False
                index += 1
                continue
            if not triple and text[index] == "\\":
                escaped = True
                index += 1
                continue
            if triple and text.startswith(char * 3, index):
                index += 3
                break
            if not triple and text[index] == char:
                index += 1
                break
            index += 1
        spans.append((start, index))
    return tuple(spans)


def _offset_in_spans(offset: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= offset < end for start, end in spans)


def _matching_brace(text: str, opening: int) -> int | None:
    depth = 0
    quote: str | None = None
    triple = False
    escaped = False
    index = opening
    while index < len(text):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and not triple:
                escaped = True
            elif triple and text.startswith(quote * 3, index):
                quote = None
                triple = False
                index += 2
            elif not triple and char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            if text.startswith(char * 3, index):
                quote = char
                triple = True
                index += 3
                continue
            quote = char
            triple = False
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _strip_comments(text: str) -> str:
    """Replace Groovy/Kotlin comments with spaces while preserving strings/newlines."""

    chars = list(text)
    index = 0
    quote: str | None = None
    triple = False
    escaped = False
    while index < len(chars):
        char = chars[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and not triple:
                escaped = True
            elif triple and text.startswith(quote * 3, index):
                quote = None
                triple = False
                index += 2
            elif not triple and char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            if text.startswith(char * 3, index):
                quote = char
                triple = True
                index += 3
                continue
            quote = char
            triple = False
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            if end < 0:
                end = len(chars)
            for position in range(index, end):
                chars[position] = " "
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                end = len(chars) - 2
            stop = min(len(chars), end + 2)
            for position in range(index, stop):
                if chars[position] != "\n":
                    chars[position] = " "
            index = stop
            continue
        index += 1
    return "".join(chars)


def _stable_read_script(path: Path) -> str:
    script = Path(os.path.abspath(path))
    if script.is_symlink() or script.is_junction():
        raise ValueError("Gradle script 不能是 symlink/junction/reparse")
    if not is_local_fixed_path(script):
        raise ValueError("Gradle script 不在本地固定磁盘")
    before = read_file_metadata(script)
    _require_plain_file_metadata(before)
    size = script.stat().st_size
    if size > _MAX_SCRIPT_BYTES:
        raise ValueError(f"Gradle script 超过 {_MAX_SCRIPT_BYTES} bytes 静态读取上限")
    text = script.read_text(encoding="utf-8-sig", errors="strict")
    after = read_file_metadata(script)
    _require_plain_file_metadata(after)
    if _metadata_key(before) != _metadata_key(after):
        raise RuntimeError("Gradle script 身份/时间在读取期间发生变化")
    return text


def _ordinary_local_directory(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or candidate.is_junction():
        raise ValueError(f"{label} 不能是 symlink/junction/reparse")
    resolved = candidate.resolve(strict=True)
    if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(os.path.abspath(resolved)):
        raise ValueError(f"{label} 存在路径重定向/reparse")
    metadata = read_file_metadata(resolved)
    if not metadata.is_directory or metadata.is_reparse_point:
        raise ValueError(f"{label} 不是普通目录")
    if not is_local_fixed_path(resolved):
        raise ValueError(f"{label} 不在本地固定磁盘")
    return resolved


def _require_plain_file_metadata(metadata: object) -> None:
    if getattr(metadata, "is_directory", True) or getattr(metadata, "is_reparse_point", True):
        raise ValueError("Gradle script 不是普通文件")
    if (
        getattr(metadata, "volume_serial", None) is None
        or getattr(metadata, "file_id", None) is None
        or getattr(metadata, "file_id_kind", None) is None
    ):
        raise RuntimeError("Gradle script 缺少稳定文件身份")


def _metadata_key(metadata: object) -> tuple[object, ...]:
    return (
        getattr(metadata, "volume_serial", None),
        getattr(metadata, "file_id", None),
        getattr(metadata, "file_id_kind", None),
        getattr(metadata, "creation_time_ns", None),
        getattr(metadata, "last_write_time_ns", None),
    )


__all__ = [
    "AndroidProjectReferenceScan",
    "AndroidProjectSdkReference",
    "scan_android_project_sdk_references",
]
