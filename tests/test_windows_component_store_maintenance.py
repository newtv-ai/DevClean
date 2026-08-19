from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.windows_component_store_maintenance as component_store
from devclean.core.windows_component_store_maintenance import (
    ComponentStoreReport,
    DismExecutableIdentity,
    cleanup_windows_component_store,
    inventory_windows_component_store,
    parse_component_store_report,
)


def _identity(seed: int = 1) -> DismExecutableIdentity:
    return DismExecutableIdentity(
        path=Path(r"C:\Windows\System32\dism.exe"),
        volume_serial=100 + seed,
        file_id=f"dism-{seed}",
        file_id_kind="128",
        last_write_time_ns=1000 + seed,
    )


def _report(
    identity: DismExecutableIdentity,
    *,
    recommended: bool = True,
    image_version: str = "10.0.26100.4946",
    size: int | None = 8 * 1024**3,
    packages: int | None = 3,
) -> ComponentStoreReport:
    return ComponentStoreReport(
        tool_identity=identity,
        dism_version="10.0.26100.5074",
        image_version=image_version,
        actual_size_bytes=size,
        reclaimable_packages=packages,
        cleanup_recommended=recommended,
        raw_output="vendor report",
    )


def _sample_output(*, recommendation: str = "Yes") -> str:
    return f"""Deployment Image Servicing and Management tool
Version: 10.0.26100.5074

Image Version: 10.0.26100.4946

[==========================100.0%==========================]

Component Store (WinSxS) information:

Windows Explorer Reported Size of Component Store : 9.57 GB
Actual Size of Component Store : 9.32 GB
    Shared with Windows : 4.34 GB
    Backups and Disabled Features : 4.98 GB
    Cache and Temporary Data : 0 bytes

Date of Last Cleanup : 2026-08-01 10:20:30
Number of Reclaimable Packages : 2
Component Store Cleanup Recommended : {recommendation}

The operation completed successfully.
"""


def test_parse_component_store_report_positive() -> None:
    identity = _identity()

    report = parse_component_store_report(_sample_output(), identity)

    assert report.tool_identity == identity
    assert report.dism_version == "10.0.26100.5074"
    assert report.image_version == "10.0.26100.4946"
    assert report.actual_size_bytes == int(9.32 * 1024**3)
    assert report.reclaimable_packages == 2
    assert report.cleanup_recommended


def test_parse_component_store_report_negative() -> None:
    report = parse_component_store_report(_sample_output(recommendation="No"), _identity())

    assert not report.cleanup_recommended


def test_parse_component_store_report_missing_or_duplicate_recommendation_fails_closed() -> None:
    missing = _sample_output().replace("Component Store Cleanup Recommended : Yes\n", "")
    duplicate = _sample_output() + "Component Store Cleanup Recommended : Yes\n"

    with pytest.raises(RuntimeError, match="cleanup recommendation"):
        parse_component_store_report(missing, _identity())
    with pytest.raises(RuntimeError, match="cleanup recommendation"):
        parse_component_store_report(duplicate, _identity())


def test_parse_component_store_report_allows_optional_size_fields_to_be_unparseable() -> None:
    output = _sample_output().replace(
        "Actual Size of Component Store : 9.32 GB",
        "Actual Size of Component Store : unknown",
    )
    output = output.replace("Number of Reclaimable Packages : 2\n", "")

    report = parse_component_store_report(output, _identity())

    assert report.actual_size_bytes is None
    assert report.reclaimable_packages is None
    assert report.cleanup_recommended


def test_inventory_non_elevated_never_runs_dism_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: identity)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: False)
    monkeypatch.setattr(
        component_store,
        "_analyze_component_store",
        lambda identity, environment: pytest.fail("analysis must not run without elevation"),
    )

    inventory = inventory_windows_component_store()

    assert not inventory.elevated
    assert inventory.report is None
    assert not inventory.cleanup_supported
    assert "不会自动提升权限" in inventory.reason


def test_inventory_uses_vendor_recommendation_without_defaulting_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    report = _report(identity, recommended=True)
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: identity)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "_analyze_component_store",
        lambda identity, environment: report,
    )

    inventory = inventory_windows_component_store()

    assert inventory.elevated
    assert inventory.report == report
    assert inventory.cleanup_supported
    assert "30 天" in inventory.reason


def test_cleanup_requires_reviewed_positive_report(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _identity()
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)

    with pytest.raises(ValueError, match="没有建议"):
        cleanup_windows_component_store(_report(identity, recommended=False))


def test_cleanup_refuses_existing_dism_activity_before_vendor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "dism_activity_running",
        lambda environment=None: True,
    )
    monkeypatch.setattr(
        component_store,
        "_run_command",
        lambda *args, **kwargs: pytest.fail(
            "cleanup must not run during existing DISM activity"
        ),
    )

    with pytest.raises(RuntimeError, match="DISM/DismHost"):
        cleanup_windows_component_store(_report(identity))


def test_cleanup_revalidates_exact_dism_and_image_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_identity = _identity(1)
    changed_identity = _identity(2)
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "dism_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: expected_identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: changed_identity)

    with pytest.raises(RuntimeError, match="可执行文件身份"):
        cleanup_windows_component_store(_report(expected_identity))


def test_cleanup_refuses_if_fresh_analysis_no_longer_recommends_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "dism_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: identity)
    monkeypatch.setattr(
        component_store,
        "_analyze_component_store",
        lambda identity, environment: _report(identity, recommended=False),
    )
    monkeypatch.setattr(
        component_store,
        "_run_command",
        lambda *args, **kwargs: pytest.fail(
            "cleanup must not run after recommendation changed"
        ),
    )

    with pytest.raises(RuntimeError, match="已不再建议"):
        cleanup_windows_component_store(_report(identity))


def test_cleanup_uses_only_bounded_startcomponentcleanup_command_and_reanalyzes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    before = _report(identity, size=9 * 1024**3, packages=4)
    after = _report(identity, recommended=False, size=7 * 1024**3, packages=0)
    analyses = [before, after]
    commands: list[list[str]] = []

    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "dism_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: identity)
    monkeypatch.setattr(
        component_store,
        "_analyze_component_store",
        lambda tool_identity, environment: analyses.pop(0),
    )

    def fake_run(
        command: list[str],
        environment: object,
        *,
        timeout: int,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout, operation
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="cleanup complete\n",
            stderr="",
        )

    monkeypatch.setattr(component_store, "_run_command", fake_run)

    result = cleanup_windows_component_store(before)

    assert commands == [
        [
            str(identity.path),
            "/Online",
            "/English",
            "/Cleanup-Image",
            "/StartComponentCleanup",
            "/NoRestart",
        ]
    ]
    assert "/ResetBase" not in commands[0]
    assert "/Quiet" not in commands[0]
    assert "/SPSuperseded" not in commands[0]
    assert result.before == before
    assert result.after == after
    assert result.reported_size_delta_bytes == 2 * 1024**3


def test_cleanup_refuses_changed_image_version_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    expected = _report(identity)
    changed = _report(identity, image_version="10.0.99999.1")
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(component_store, "is_process_elevated", lambda: True)
    monkeypatch.setattr(
        component_store,
        "dism_activity_running",
        lambda environment=None: False,
    )
    monkeypatch.setattr(
        component_store,
        "dism_executable",
        lambda environment=None: identity.path,
    )
    monkeypatch.setattr(component_store, "_dism_identity", lambda path: identity)
    monkeypatch.setattr(
        component_store,
        "_analyze_component_store",
        lambda identity, environment: changed,
    )

    with pytest.raises(RuntimeError, match="映像身份"):
        cleanup_windows_component_store(expected)


def test_dism_activity_check_fails_closed_on_process_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    monkeypatch.setattr(
        component_store.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="denied",
        ),
    )

    assert component_store.dism_activity_running(
        {"DEVCLEAN_TASKLIST_EXE": "tasklist-test"}
    )


def test_dism_activity_check_detects_dismhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(component_store, "_WINDOWS", True)
    output = '"DismHost.exe","4242","Console","1","10,000 K"\n'
    monkeypatch.setattr(
        component_store.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=output,
            stderr="",
        ),
    )

    assert component_store.dism_activity_running(
        {"DEVCLEAN_TASKLIST_EXE": "tasklist-test"}
    )


def test_dism_identity_rejects_reparse_or_non_local_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = SimpleNamespace(
        is_directory=False,
        is_reparse_point=True,
        volume_serial=1,
        file_id="id",
        file_id_kind="128",
        last_write_time_ns=1,
    )
    monkeypatch.setattr(component_store, "read_file_metadata", lambda path: metadata)
    monkeypatch.setattr(component_store, "is_local_fixed_path", lambda path: True)

    with pytest.raises(RuntimeError, match="普通可执行文件"):
        component_store._dism_identity(Path("dism-test.exe"))
