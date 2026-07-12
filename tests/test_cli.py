from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from reclaimer.adapters.base import (
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from reclaimer.cli import main as cli
from reclaimer.core.models import (
    Confidence,
    FileIdentity,
    ProvenanceClass,
    Resource,
    RiskTier,
    ScanStatus,
    SemanticType,
    SizeValue,
)
from reclaimer.core.state import StateStore


def test_doctor_json_is_inventory_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "collect_diagnostics",
        lambda: {
            "platform": "Windows-test",
            "python_version": "3.13",
            "process_elevated": False,
            "state_integrity": "not_created",
            "safety_message": "Inventory-only milestone",
            "execution_allowed": False,
        },
    )
    assert cli.main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_allowed"] is False


def test_doctor_rejects_elevated_main_process(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "collect_diagnostics",
        lambda: {
            "platform": "Windows-test",
            "python_version": "3.13",
            "process_elevated": True,
            "state_integrity": "ok",
            "safety_message": "exit",
        },
    )
    assert cli.main(["doctor"]) == 2
    assert "Elevated: True" in capsys.readouterr().out


def test_doctor_fails_closed_when_elevation_cannot_be_checked(monkeypatch, capsys) -> None:
    def fail_diagnostics():
        raise OSError("token unavailable")

    monkeypatch.setattr(cli, "collect_diagnostics", fail_diagnostics)

    assert cli.main(["doctor"]) == 2
    assert "refusing to continue" in capsys.readouterr().err


def test_guides_are_external_and_report_only(capsys) -> None:
    assert cli.main(["guides", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["externally_executed"] is True
    assert payload["command"][0] == "DISM.exe"
    assert "does not execute" in payload["safety_boundary"]


def test_report_without_state_returns_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path))
    assert cli.main(["report", "--latest"]) == 1
    assert "No stored scan" in capsys.readouterr().err


def test_non_doctor_commands_refuse_elevated_main_process(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "is_process_elevated", lambda: True)

    assert cli.main(["guides", "--json"]) == 2
    assert "without elevation" in capsys.readouterr().err


class _InteractiveStdin:
    def isatty(self) -> bool:
        return True


def test_recycle_requires_exact_interactive_confirmation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path / "state"))
    target_path = tmp_path / "scan-root" / "cache.bin"
    target_path.parent.mkdir()
    target_path.write_bytes(b"fixture")
    resource = Resource(
        candidate_id="candidate_recycle_cli",
        adapter_id="filesystem",
        display_name="Filesystem file",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=str(target_path),
        logical_size=SizeValue(7, Confidence.EXACT),
        identity=FileIdentity(
            volume_serial="000000000000002a",
            file_id="ab" * 16,
            file_id_kind="file_id_128",
            link_count=1,
            attributes=32,
            creation_time_ns=100,
            last_write_time_ns=200,
        ),
    )
    with StateStore() as store:
        scan_id = store.create_scan([str(target_path.parent)])
        store.add_resources(scan_id, [resource])
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

    monkeypatch.setattr(cli.sys, "stdin", _InteractiveStdin())
    monkeypatch.setattr("builtins.input", lambda _prompt="": f"RECYCLE {scan_id}")
    observed: list[str] = []

    def fake_recycle(targets, _recycler):
        observed.extend(target.candidate_id for target in targets)
        return tuple(targets)

    monkeypatch.setattr(cli, "recycle_targets", fake_recycle)
    assert cli.main(["recycle", scan_id, "--select", resource.candidate_id, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert observed == [resource.candidate_id]
    assert payload["undo_capability"] == "RECYCLE_BIN"
    assert payload["safety_boundary"]["permanent_delete"] is False


def test_recycle_rejects_noninteractive_input(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path / "state"))

    assert cli.main(["recycle", "scan_fixture", "--select", "candidate_fixture"]) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_elevation_probe_failure_is_fail_closed(monkeypatch, capsys) -> None:
    def fail_probe() -> bool:
        raise OSError("token unavailable")

    monkeypatch.setattr(cli, "is_process_elevated", fail_probe)

    assert cli.main(["guides"]) == 2
    assert "refusing to continue" in capsys.readouterr().err


def test_scan_streams_to_state_and_remains_non_actionable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "input"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "one.bin").write_bytes(b"one")
    (nested / "two.bin").write_bytes(b"two-two")
    data = tmp_path / "state-data"
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(data))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan", "--root", str(root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "COMPLETED"
    assert result["summary"]["files"] == 2
    assert result["summary"]["filesystem_resources"] == 2
    assert result["safety_boundary"]["actionable"] is False

    with StateStore(data / "state" / "reclaimer.db") as store:
        resources = store.list_resources(result["scan_id"])
        assert len(resources) == 3  # two files plus one REPORT_ONLY Windows guide
        assert all(resource["actionable"] is False for resource in resources)
        file_resources = [
            resource for resource in resources if resource["adapter_id"] == "filesystem"
        ]
        assert {resource["logical_size"]["value"] for resource in file_resources} == {3, 7}
        assert all(resource["semantic_type"] == "UNKNOWN" for resource in file_resources)
        assert all(resource["risk_tier"] == "RED" for resource in file_resources)


def test_scan_rejects_unc_root_before_opening_state(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan", "--root", r"\\server\share"]) == 2
    assert "network and device roots" in capsys.readouterr().err
    assert not (tmp_path / "state" / "reclaimer.db").exists()


def test_full_path_report_warns_but_still_redacts_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    database_root = tmp_path / "state-data"
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(database_root))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)
    with StateStore(database_root / "state" / "reclaimer.db") as store:
        scan_id = store.create_scan([r"C:\Users\Alice\project"])
        store.add_resource(
            scan_id,
            Resource(
                candidate_id="candidate_fixture",
                adapter_id="filesystem",
                display_name="Filesystem file",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=r"C:\Users\Alice\token=supersecret",
            ),
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

    assert cli.main(["report", scan_id, "--format", "json", "--full-paths"]) == 0
    captured = capsys.readouterr()
    assert "Alice" in captured.out
    assert "supersecret" not in captured.out
    assert "<REDACTED>" in captured.out
    assert "full local paths" in captured.err


def test_scan_requires_a_root_or_adapter(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan"]) == 2
    assert "at least one --root or --adapter" in capsys.readouterr().err


def test_python_option_requires_pip_adapter(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan", "--adapter", "uv", "--python", sys.executable]) == 2
    assert "requires --adapter pip" in capsys.readouterr().err


def test_docker_path_requires_docker_adapter(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan", "--adapter", "uv", "--docker", sys.executable]) == 2
    assert "requires --adapter docker" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("adapter", "option"),
    (("pip", "--python"), ("conda", "--conda"), ("docker", "--docker")),
)
def test_explicit_adapter_executable_rejects_batch_path_before_opening_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    option: str,
) -> None:
    data = tmp_path / "state-data"
    batch = tmp_path / "untrusted.CMD"
    batch.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(data))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)

    assert cli.main(["scan", "--adapter", adapter, option, str(batch)]) == 2
    assert "must be a .exe file" in capsys.readouterr().err
    assert not (data / "state" / "reclaimer.db").exists()


def test_filesystem_only_and_loopback_adapters_are_explicitly_registered() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "scan",
            "--adapter",
            "pnpm",
            "--adapter",
            "ollama",
            "--adapter",
            "vscode",
        ]
    )

    assert [adapter.id for adapter in cli._build_adapters(args)] == [
        "pnpm",
        "ollama",
        "vscode",
    ]


def test_report_console_format_remains_part_of_the_cli_contract() -> None:
    args = cli.build_parser().parse_args(
        ["report", "scan_fixture", "--format", "console"]
    )
    assert args.format == "console"


def test_adapter_only_scan_persists_resources_issues_and_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    class FakeAdapter:
        id = "pip"

        def inventory(self, context) -> InventoryResult:
            return InventoryResult(
                self.id,
                ProbeResult(self.id, ProbeStatus.AVAILABLE, "26.1.1"),
                resources=(
                    Resource(
                        candidate_id="candidate_adapter_fixture",
                        adapter_id=self.id,
                        display_name="pip cache root",
                        semantic_type=SemanticType.REBUILDABLE_CACHE,
                        risk_tier=RiskTier.YELLOW,
                        provenance_class=ProvenanceClass.UNKNOWN,
                        vendor_locator="pip:fixture",
                        path=r"C:\Users\Alice\pip-cache",
                    ),
                ),
                issues=(AdapterIssue("PARTIAL_INDEX", "Synthetic partial index."),),
            )

    data = tmp_path / "state-data"
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(data))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)
    monkeypatch.setattr(cli, "_build_adapters", lambda args: [FakeAdapter()])

    assert cli.main(["scan", "--adapter", "pip", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["adapters"] == [
        {
            "adapter_id": "pip",
            "evidence": 0,
            "issues": 1,
            "resources": 1,
            "status": "AVAILABLE",
            "version": "26.1.1",
        }
    ]
    with StateStore(data / "state" / "reclaimer.db") as store:
        resources = store.list_resources(result["scan_id"])
        errors = store.list_scan_errors(result["scan_id"])
        adapter_runs = list(store.iter_adapter_runs(result["scan_id"]))
    assert any(item["candidate_id"] == "candidate_adapter_fixture" for item in resources)
    assert errors[0]["kind"] == "ADAPTER_PIP_PARTIAL_INDEX"
    assert len(adapter_runs) == 1
    assert adapter_runs[0]["adapter_id"] == "pip"
    assert adapter_runs[0]["status"] == "AVAILABLE"


def test_plan_create_and_show_are_report_only_and_id_scoped(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data = tmp_path / "state-data"
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(data))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)
    candidate_id = "candidate_plan_fixture"
    with StateStore(data / "state" / "reclaimer.db") as store:
        scan_id = store.create_scan([r"C:\fixture"])
        store.add_resource(
            scan_id,
            Resource(
                candidate_id=candidate_id,
                adapter_id="filesystem",
                display_name="Selected report-only file",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=r"C:\private\selected.bin",
            ),
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

    assert (
        cli.main(
            [
                "plan",
                "create",
                scan_id,
                "--select",
                candidate_id,
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    plan = created["plan"]
    assert plan["executable"] is False
    assert plan["actions"][0]["kind"] == "REPORT_ONLY"
    assert plan["actions"][0]["enabled"] is False
    assert r"C:\private" not in json.dumps(plan)

    assert cli.main(["plan", "show", plan["plan_id"], "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["plan"] == plan
    assert shown["safety_boundary"]["executable"] is False


def test_plan_create_rejects_candidate_outside_selected_scan(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data = tmp_path / "state-data"
    monkeypatch.setenv("RECLAIMER_DATA_DIR", str(data))
    monkeypatch.setattr(cli, "is_process_elevated", lambda: False)
    with StateStore(data / "state" / "reclaimer.db") as store:
        scan_id = store.create_scan([r"C:\fixture"])
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

    assert (
        cli.main(
            [
                "plan",
                "create",
                scan_id,
                "--select",
                "candidate_not_in_scan",
            ]
        )
        == 1
    )
    assert "not part of scan" in capsys.readouterr().err
