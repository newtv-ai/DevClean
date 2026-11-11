"""Scan hot-path speedups must not change any decision (see docs/scan-performance-ui-2026-11-10.md).

Everything runs on paths and folders inside ``tmp_path``.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import devclean.core as core
import devclean.core.application_cleanup as application_cleanup
import devclean.core.installer_files as installer_files
from devclean.core._application_cleanup_impl import ApplicationCleanupRule, scan_root_snapshot
from devclean.core.chrome_cleanup import _CHROME_UPDATER_RULES
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.review_routing import route_unresolved_file_to_ai
from devclean.core.triage import ReviewLane, TriageSession, triage_directory, triage_file
from devclean.core.user_rules import default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app

_NOW = datetime(2026, 11, 10, tzinfo=UTC)
_OLD_NS = int((_NOW - timedelta(days=400)).timestamp() * 1_000_000_000)


@pytest.fixture
def tool_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A profile in which every audited application has a configured location."""

    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    folders = {
        "USERPROFILE": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": local,
        "TEMP": local / "Temp",
        "TMP": local / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
        "SYSTEMROOT": tmp_path / "Windows",
        "WINDIR": tmp_path / "Windows",
        "PROGRAMFILES": tmp_path / "Program Files",
        "PROGRAMFILES(X86)": tmp_path / "Program Files (x86)",
        "CODEX_HOME": home / ".codex-custom",
        "CLAUDE_CONFIG_DIR": home / ".claude-custom",
        "NPM_CONFIG_CACHE": tmp_path / "npm-cache",
        "PIP_CACHE_DIR": tmp_path / "pip-cache",
        "UV_CACHE_DIR": tmp_path / "uv-cache",
        "GOPATH": tmp_path / "gopath",
        "GOMODCACHE": tmp_path / "gomod",
        "CARGO_HOME": tmp_path / "cargo",
        "GRADLE_USER_HOME": tmp_path / "gradle",
        "ANDROID_HOME": tmp_path / "android",
        "ANDROID_AVD_HOME": tmp_path / "avd",
        "HF_HOME": tmp_path / "hf",
        "OLLAMA_MODELS": tmp_path / "ollama",
        "PLAYWRIGHT_BROWSERS_PATH": tmp_path / "playwright",
        "PUPPETEER_CACHE_DIR": tmp_path / "puppeteer",
        "CYPRESS_CACHE_FOLDER": tmp_path / "cypress",
        "ELECTRON_CACHE": tmp_path / "electron",
        "NUGET_PACKAGES": tmp_path / "nuget",
        "CONDA_PKGS_DIRS": tmp_path / "conda-pkgs",
        "BUN_INSTALL_CACHE_DIR": tmp_path / "bun-cache",
        "PNPM_HOME": tmp_path / "pnpm",
        "DOCKER_CONFIG": tmp_path / "docker",
    }
    for name, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(folder))
    monkeypatch.setenv("PATH", "")
    return home


def _all_rules() -> list[ApplicationCleanupRule]:
    found: list[ApplicationCleanupRule] = []
    for info in pkgutil.iter_modules(core.__path__):
        module = importlib.import_module(f"devclean.core.{info.name}")
        for value in vars(module).values():
            members = value if isinstance(value, tuple) else (value,)
            found.extend(item for item in members if isinstance(item, ApplicationCleanupRule))
    return found


def _concrete(pattern: str) -> list[str]:
    start = pattern.find("{")
    if start >= 0:
        end = pattern.find("}", start)
        return [
            text
            for choice in pattern[start + 1 : end].split(",")
            for text in _concrete(pattern[:start] + choice + pattern[end + 1 :])
        ]
    return [re.sub(r"\[[^\]]*\]", "a", pattern).replace("*", "x1").replace("?", "a")]


def test_skipping_applications_that_cannot_hold_a_path_changes_no_decision(
    tool_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = _all_rules()
    relatives = sorted({text for rule in rules for text in _concrete(rule.relative_pattern)})
    names = {
        "tmpclaude-ab12-cwd", "npm-debug.log", "package.json", "Cargo.toml", "config.toml",
        "go.mod", "pom.xml", "bun.lockb", "yarn.lock", "pnpm-lock.yaml", "packages.config",
        ".condarc", r".mvn\maven.config", r".cargo\config", r"conda-meta\history",
        r".bun\cache\x.bin", r".yarn\cache\x.zip", "settings.json", "claude.exe",
    }  # fmt: skip
    real = application_cleanup._applications_not_holding
    with scan_root_snapshot():
        index = application_cleanup._application_scope_index()
        roots = sorted(index.by_folder)
        probes = {root for root in roots}
        generator = random.Random(7)
        for root in roots:
            for relative in generator.sample(relatives, min(len(relatives), 12)):
                probes.add(root + "\\" + relative)
                probes.add(root + "\\" + relative + "\\f.bin")
        folders = [*roots, str(tool_profile), str(tool_profile / "source" / "app")]
        probes.update(folder + "\\" + name for folder in folders for name in names)
        skipped = 0
        for probe in sorted(probes):
            skipped += len(real(probe))
            fast = application_cleanup.evaluate_application_path(
                probe, logical_size=4096, last_used=_NOW - timedelta(days=400), now=_NOW
            )
            monkeypatch.setattr(
                application_cleanup, "_applications_not_holding", lambda _p: frozenset()
            )
            full = application_cleanup.evaluate_application_path(
                probe, logical_size=4096, last_used=_NOW - timedelta(days=400), now=_NOW
            )
            monkeypatch.setattr(application_cleanup, "_applications_not_holding", real)
            assert repr(fast) == repr(full), probe
    assert skipped > len(probes)  # the index does skip applications


def _unknown_file(path: Path, size: int) -> ScanRecord:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return ScanRecord(
        root=str(path.parents[1]),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=2,
        logical_size=size,
        last_write_time_ns=_OLD_NS,
    )


def test_live_preview_classifies_each_retained_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = default_rules()
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    records = [_unknown_file(tmp_path / "work" / f"data-{i}.bin", i + 1) for i in range(5)]
    items = [
        route_unresolved_file_to_ai(
            triage_file(
                record,
                delete_config=rules.delete.classification,
                keep_config=rules.keep.classification,
                now=_NOW,
                # tmp_path itself lies in the system temp folder
                temp_root=tmp_path / "elsewhere",
            )
        )
        for record in records
    ]
    assert all(item.lane is ReviewLane.AI_REVIEW for item in items)
    classified: list[str] = []
    real_bucket = app._item_bucket

    def counting_bucket(item: object, *args: object) -> int:
        classified.append(item.path)  # type: ignore[attr-defined]
        return real_bucket(item, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(app, "_item_bucket", counting_bucket)
    for item in items[:3]:
        session.add(item)
    app._rows_of(session, rules)
    for item in items[3:]:
        session.add(item)
    second = app._rows_of(session, rules)

    assert sorted(classified) == sorted(item.path for item in items)
    app._PREVIEW_MEMO.pop(session)
    assert app._rows_of(session, rules) == second  # same answer as a fresh preview


def test_installer_markers_are_listed_once_per_folder_in_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "unins000.dat").write_bytes(b"inno")
    listed: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path: object) -> object:
        listed.append(str(path))
        return real_scandir(path)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", counting_scandir)
    with scan_root_snapshot():
        assert all(installer_files.has_installed_program_markers(folder) for _ in range(5))
    assert len(listed) == 1
    # Outside a run every call looks again, as before.
    assert installer_files.has_installed_program_markers(folder)
    assert len(listed) == 2


def test_nothing_in_a_deny_listed_system_folder_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = default_rules()
    anchor = Path(tmp_path.anchor)
    system = anchor / "Program Files (x86)"
    local = tmp_path / "home" / "AppData" / "Local"
    monkeypatch.setenv("PROGRAMFILES(X86)", str(system))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    crx_rule = next(rule for rule in _CHROME_UPDATER_RULES if rule.rule_id.endswith("crx-cache"))

    def offered_file(crx: Path) -> ScanRecord:
        return ScanRecord(
            root=str(anchor),
            path=str(crx / "payload.crx3"),
            kind=ScanRecordKind.FILE,
            depth=5,
            logical_size=64 * 1024 * 1024,
            last_write_time_ns=_OLD_NS,
        )

    def offered_directory(crx: Path) -> tuple[ScanRecord, tuple[KnownCleanupRoot, ...]]:
        record = ScanRecord(
            root=str(anchor),
            path=str(crx),
            kind=ScanRecordKind.DIRECTORY,
            depth=4,
            volume_serial=1,
            file_id="1" * 32,
            file_id_kind="file_id_128",
            link_count=1,
            attributes=0x10,
            creation_time_ns=1,
            last_write_time_ns=_OLD_NS,
        )
        known = KnownCleanupRoot(
            path=crx,
            category=CleanupCategory.BROWSER_CACHE,
            policy=CleanupPolicy.VENDOR_MANAGED,
            label="Chrome updater cache",
            delete_root_itself=True,
            application_rule=crx_rule,
        )
        return record, (known,)

    for crx, offered in (
        (local / "Google" / "GoogleUpdater" / "crx_cache", True),
        (system / "Google" / "GoogleUpdater" / "crx_cache", False),
    ):
        file_item = triage_file(
            offered_file(crx),
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
            now=_NOW,
        )
        record, known = offered_directory(crx)
        directory_item = triage_directory(
            record,
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
            known_roots=known,
        )
        assert directory_item is not None
        for item in (file_item, directory_item):
            assert (item.lane is not ReviewLane.REPORT_ONLY) is offered, (crx, item.reason)
            if not offered:
                assert "system_location" in item.tags
                assert "Windows 系统保护位置" in item.reason
                assert route_unresolved_file_to_ai(item).lane is ReviewLane.REPORT_ONLY
