"""DevClean command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from devclean.adapters.base import AdapterContext, InventoryAdapter
from devclean.adapters.catalog import get_descriptor
from devclean.adapters.command import run_query
from devclean.adapters.conda import CondaCacheAdapter
from devclean.adapters.docker import DockerAdapter
from devclean.adapters.huggingface import HuggingFaceAdapter
from devclean.adapters.npm import NpmCacheAdapter
from devclean.adapters.ollama import OllamaAdapter
from devclean.adapters.pip_cache import PipCacheAdapter
from devclean.adapters.pnpm import PnpmStoreAdapter
from devclean.adapters.uv_cache import UvCacheAdapter
from devclean.adapters.vscode import VSCodeExtensionAdapter
from devclean.adapters.windows_maintenance import (
    COMPONENT_STORE_ANALYSIS,
    report_only_resources,
)
from devclean.core.doctor import collect_diagnostics
from devclean.core.models import Resource, ScanStatus, utc_now
from devclean.core.policy import build_inventory_plan_from_records
from devclean.core.reporting import (
    iter_json_report,
    iter_markdown_report,
    write_report_stream,
)
from devclean.core.state import StateStore
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.process import validate_executable_path
from devclean.platform.windows.security import is_process_elevated
from devclean.scanner import CancellationToken, ScanOptions, ScanRecordKind, ScanStats, scan_roots
from devclean.scanner.resources import file_record_to_resource, record_to_scan_error

_SCAN_BATCH_SIZE = 512


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DevClean",
        description="Evidence-first, read-only disk inventory for Windows.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect the local safety boundary")
    doctor.add_argument("--json", action="store_true", help="Emit structured JSON")

    guides = subparsers.add_parser(
        "guides", help="Show report-only official maintenance commands"
    )
    guides.add_argument("--json", action="store_true", help="Emit structured JSON")

    scan = subparsers.add_parser(
        "scan", help="Inventory local roots without following reparse or Cloud boundaries"
    )
    scan.add_argument(
        "--root",
        action="append",
        metavar="PATH",
        help="Local file or directory to scan; repeat for multiple roots",
    )
    scan.add_argument(
        "--adapter",
        action="append",
        choices=(
            "huggingface",
            "pip",
            "uv",
            "conda",
            "npm",
            "pnpm",
            "docker",
            "ollama",
            "vscode",
        ),
        help="Run an explicitly selected read-only inventory adapter; repeat as needed",
    )
    scan.add_argument(
        "--python",
        action="append",
        type=Path,
        metavar="PATH",
        help="Verified local python.exe for the pip adapter; repeat as needed",
    )
    scan.add_argument("--conda", type=Path, metavar="PATH", help="Verified local conda.exe path")
    scan.add_argument("--docker", type=Path, metavar="PATH", help="Verified local docker.exe path")
    scan.add_argument("--node", type=Path, metavar="PATH", help="Verified local node.exe for npm")
    scan.add_argument(
        "--npm-cli", type=Path, metavar="PATH", help="npm-cli.js paired with --node"
    )
    scan.add_argument("--json", action="store_true", help="Emit a structured scan summary")
    scan.add_argument(
        "--progress",
        action="store_true",
        help="Write bounded progress updates to stderr",
    )

    report = subparsers.add_parser(
        "report", help="Export a stored scan; exports are not executable"
    )
    scan_group = report.add_mutually_exclusive_group(required=True)
    scan_group.add_argument("scan_id", nargs="?", help="Stored scan identifier")
    scan_group.add_argument("--latest", action="store_true", help="Use the latest stored scan")
    report.add_argument(
        "--format",
        choices=("console", "json", "markdown"),
        default="markdown",
        help=(
            "console is the streamed human-readable view; markdown uses the same safe text form"
        ),
    )
    report.add_argument("--output", type=Path, help="Write to a local file instead of stdout")
    report.add_argument(
        "--full-paths",
        action="store_true",
        help="Show full paths and vendor locators; exported reports may contain sensitive names",
    )

    plans = subparsers.add_parser(
        "plan", help="Create or inspect a non-executable REPORT_ONLY review plan"
    )
    plan_subparsers = plans.add_subparsers(dest="plan_command", required=True)
    plan_create = plan_subparsers.add_parser(
        "create", help="Select stored candidates by opaque ID for report-only review"
    )
    plan_create.add_argument("scan_id", help="Completed stored scan identifier")
    plan_create.add_argument(
        "--select",
        action="append",
        required=True,
        metavar="CANDIDATE_ID",
        help="Stored candidate ID; repeat for multiple candidates (maximum 256)",
    )
    plan_create.add_argument("--json", action="store_true", help="Emit structured JSON")
    plan_show = plan_subparsers.add_parser(
        "show", help="Show a stored report-only plan by opaque plan ID"
    )
    plan_show.add_argument("plan_id", help="Stored plan identifier")
    plan_show.add_argument("--json", action="store_true", help="Emit structured JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor(as_json=bool(args.json))
    if _elevated_main_process():
        return 2
    if args.command == "guides":
        return _guides(as_json=bool(args.json))
    if args.command == "scan":
        return _scan(args)
    if args.command == "report":
        return _report(args)
    if args.command == "plan" and args.plan_command == "create":
        return _plan_create(args)
    if args.command == "plan" and args.plan_command == "show":
        return _plan_show(args)
    parser.error(f"unsupported command: {args.command}")


def _elevated_main_process() -> bool:
    try:
        elevated = is_process_elevated()
    except OSError as error:
        print(f"Unable to verify process elevation; refusing to continue: {error}", file=sys.stderr)
        return True
    if elevated:
        print(
            "DevClean's main process must run without elevation. "
            "Close this terminal and restart it normally.",
            file=sys.stderr,
        )
    return elevated


def _doctor(*, as_json: bool) -> int:
    try:
        diagnostics = collect_diagnostics()
    except OSError as error:
        print(f"Unable to verify process elevation; refusing to continue: {error}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("DevClean doctor")
        print(f"  Platform: {diagnostics['platform']}")
        print(f"  Python: {diagnostics['python_version']}")
        print(f"  Elevated: {diagnostics['process_elevated']}")
        print(f"  State: {diagnostics['state_integrity']}")
        execution_platform = diagnostics.get("future_execution_platform")
        if isinstance(execution_platform, dict):
            print(
                "  Future execution platform: "
                f"{execution_platform.get('status', 'UNKNOWN')}"
            )
        print(f"  Safety: {diagnostics['safety_message']}")
    return 2 if diagnostics["process_elevated"] else 0


def _guides(*, as_json: bool) -> int:
    guide = COMPONENT_STORE_ANALYSIS
    payload = {
        "guide_id": guide.guide_id,
        "title": guide.title,
        "command": list(guide.command),
        "display_command": guide.display_command,
        "description": guide.description,
        "requires_administrator": guide.requires_administrator,
        "externally_executed": guide.externally_executed,
        "official_source": guide.official_source,
        "safety_boundary": (
            "DevClean does not execute, elevate, journal, verify, or audit this command."
        ),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(guide.title)
        print(f"  {guide.display_command}")
        print(f"  {guide.description}")
        print(f"  Source: {guide.official_source}")
        print("  REPORT_ONLY: run it yourself in an administrator terminal if you choose.")
    return 0


def _scan(args: argparse.Namespace) -> int:
    try:
        roots = _normalize_scan_roots(args.root or ())
        adapters = _build_adapters(args)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not roots and not adapters:
        print("scan requires at least one --root or --adapter", file=sys.stderr)
        return 2

    last_stats = ScanStats()
    token = CancellationToken()

    def on_progress(stats: ScanStats) -> None:
        nonlocal last_stats
        last_stats = stats
        if args.progress:
            print(
                f"\rfiles={stats.files} boundaries={stats.boundaries} "
                f"errors={stats.errors} allocated={stats.allocated_bytes}",
                end="",
                file=sys.stderr,
                flush=True,
            )

    try:
        with StateStore() as store:
            scan_id = store.create_scan(roots)
            guides = report_only_resources()
            pending_resources: list[Resource] = list(guides)
            pending_errors: list[tuple[str, str, str | None]] = []
            filesystem_resources = 0
            adapter_summary: list[dict[str, object]] = []

            def flush() -> None:
                if pending_resources:
                    store.add_resources(scan_id, pending_resources)
                    pending_resources.clear()
                if pending_errors:
                    store.add_scan_errors(scan_id, pending_errors)
                    pending_errors.clear()

            try:
                for record in scan_roots(
                    roots,
                    ScanOptions(
                        include_directories=False,
                        progress_interval=_SCAN_BATCH_SIZE,
                    ),
                    cancel=token,
                    progress=on_progress,
                ):
                    if record.kind is ScanRecordKind.FILE:
                        pending_resources.append(file_record_to_resource(record))
                        filesystem_resources += 1
                    else:
                        observation_error = record_to_scan_error(record)
                        if observation_error is not None:
                            pending_errors.append(observation_error)

                    if (
                        len(pending_resources) >= _SCAN_BATCH_SIZE
                        or len(pending_errors) >= _SCAN_BATCH_SIZE
                    ):
                        flush()

                if adapters:
                    adapter_context = AdapterContext(
                        scan_id,
                        EvidenceStore(scan_id),
                        run_query,
                    )
                    for adapter in adapters:
                        descriptor = get_descriptor(adapter.id)
                        if descriptor is None:
                            raise RuntimeError(
                                f"adapter is not registered in the built-in catalog: {adapter.id}"
                            )
                        if args.progress:
                            print(f"\nadapter={adapter.id} inventory", file=sys.stderr)
                        run_started_at = utc_now()
                        try:
                            adapter_result = adapter.inventory(adapter_context)
                        except BaseException as error:
                            run_finished_at = utc_now()
                            with suppress(sqlite3.Error):
                                store.add_adapter_run(
                                    scan_id=scan_id,
                                    adapter_id=adapter.id,
                                    status="ERROR",
                                    version=None,
                                    effect_class=descriptor.effect_class,
                                    started_at=run_started_at,
                                    finished_at=run_finished_at,
                                    payload={
                                        "failure_type": type(error).__name__,
                                        "completed": False,
                                    },
                                )
                            raise
                        run_finished_at = utc_now()
                        # Persist bounded, redacted evidence before any resource or
                        # adapter-run row is allowed to reference it.
                        for evidence in adapter_result.evidence:
                            store.add_evidence(evidence)
                        for resource in adapter_result.resources:
                            pending_resources.append(resource)
                            if len(pending_resources) >= _SCAN_BATCH_SIZE:
                                flush()
                        for issue in adapter_result.issues:
                            pending_errors.append(
                                (
                                    f"ADAPTER_{adapter.id.upper()}_{issue.code}",
                                    issue.message,
                                    None,
                                )
                            )
                        flush()
                        store.add_adapter_run(
                            scan_id=scan_id,
                            adapter_id=adapter.id,
                            status=adapter_result.probe.status.value,
                            version=adapter_result.probe.version,
                            effect_class=descriptor.effect_class,
                            started_at=run_started_at,
                            finished_at=run_finished_at,
                            payload={
                                "completed": True,
                                "executable": adapter_result.probe.executable,
                                "detail": adapter_result.probe.detail,
                                "resources": len(adapter_result.resources),
                                "issues": [
                                    {
                                        "code": issue.code,
                                        "message": issue.message,
                                        "fatal": issue.fatal,
                                    }
                                    for issue in adapter_result.issues
                                ],
                                "evidence_ids": [
                                    evidence.evidence_id
                                    for evidence in adapter_result.evidence
                                ],
                            },
                        )
                        adapter_summary.append(
                            {
                                "adapter_id": adapter.id,
                                "status": adapter_result.probe.status.value,
                                "version": adapter_result.probe.version,
                                "resources": len(adapter_result.resources),
                                "issues": len(adapter_result.issues),
                                "evidence": len(adapter_result.evidence),
                            }
                        )

                flush()
                summary = asdict(last_stats)
                summary.update(
                    {
                        "filesystem_resources": filesystem_resources,
                        "report_only_guides": len(guides),
                        "adapters": adapter_summary,
                    }
                )
                status = (
                    ScanStatus.CANCELLED if last_stats.cancelled else ScanStatus.COMPLETED
                )
                store.finish_scan(scan_id, status, summary)
            except KeyboardInterrupt:
                token.cancel()
                flush()
                summary = asdict(last_stats)
                summary.update(
                    {
                        "cancelled": True,
                        "completed": False,
                        "filesystem_resources": filesystem_resources,
                        "report_only_guides": len(guides),
                        "adapters": adapter_summary,
                    }
                )
                store.finish_scan(scan_id, ScanStatus.CANCELLED, summary)
                if args.progress:
                    print(file=sys.stderr)
                print(f"Scan cancelled safely: {scan_id}", file=sys.stderr)
                return 130
            except Exception as error:
                summary = asdict(last_stats)
                summary["failure"] = type(error).__name__
                with suppress(KeyError, OSError, RuntimeError, sqlite3.Error):
                    store.finish_scan(scan_id, ScanStatus.FAILED, summary)
                if args.progress:
                    print(file=sys.stderr)
                print(f"Scan failed: {error}", file=sys.stderr)
                return 1
    except (OSError, RuntimeError, sqlite3.Error) as error:
        print(f"Unable to open local state: {error}", file=sys.stderr)
        return 1

    if args.progress:
        print(file=sys.stderr)
    result = {
        "scan_id": scan_id,
        "status": status.value,
        "roots": roots,
        "summary": summary,
        "safety_boundary": {
            "actionable": False,
            "statement": "Inventory only; no cleaning or maintenance action was created.",
        },
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Scan stored: {scan_id}")
        print(
            f"  Files: {last_stats.files}; boundaries: {last_stats.boundaries}; "
            f"errors: {last_stats.errors}"
        )
        print("  Safety: inventory only; no cleaning action was created.")
    return 0


def _normalize_scan_roots(values: Sequence[str]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value.strip():
            raise ValueError("scan roots cannot be empty")
        expanded = os.path.expanduser(value)
        root = os.path.abspath(expanded)
        if root.startswith((r"\\", "//")):
            raise ValueError(f"network and device roots are not supported: {value}")
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            roots.append(root)
            seen.add(key)
    return roots


def _build_adapters(args: argparse.Namespace) -> list[InventoryAdapter]:
    selected = list(dict.fromkeys(args.adapter or ()))
    interpreters = list(dict.fromkeys(args.python or ()))
    if interpreters and "pip" not in selected:
        raise ValueError("--python requires --adapter pip")
    if args.conda is not None and "conda" not in selected:
        raise ValueError("--conda requires --adapter conda")
    if args.docker is not None and "docker" not in selected:
        raise ValueError("--docker requires --adapter docker")
    if (args.node is None) != (args.npm_cli is None):
        raise ValueError("--node and --npm-cli must be supplied together")
    if args.node is not None and "npm" not in selected:
        raise ValueError("--node/--npm-cli require --adapter npm")
    try:
        verified_interpreters = [
            validate_executable_path(interpreter)
            for interpreter in (interpreters or [Path(sys.executable)])
        ]
        verified_conda = (
            None if args.conda is None else validate_executable_path(args.conda)
        )
        verified_docker = (
            None if args.docker is None else validate_executable_path(args.docker)
        )
        verified_node = None if args.node is None else validate_executable_path(args.node)
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid query executable: {error}") from error
    adapters: list[InventoryAdapter] = []
    for adapter_id in selected:
        if adapter_id == "huggingface":
            adapters.append(HuggingFaceAdapter())
        elif adapter_id == "uv":
            adapters.append(UvCacheAdapter())
        elif adapter_id == "pip":
            for interpreter in verified_interpreters:
                adapters.append(PipCacheAdapter(interpreter))
        elif adapter_id == "conda":
            adapters.append(CondaCacheAdapter(verified_conda))
        elif adapter_id == "npm":
            adapters.append(NpmCacheAdapter(verified_node, args.npm_cli))
        elif adapter_id == "pnpm":
            adapters.append(PnpmStoreAdapter())
        elif adapter_id == "docker":
            adapters.append(DockerAdapter(verified_docker))
        elif adapter_id == "ollama":
            adapters.append(OllamaAdapter())
        elif adapter_id == "vscode":
            adapters.append(VSCodeExtensionAdapter())
        else:
            raise ValueError(f"adapter is not implemented: {adapter_id}")
    return adapters


def _report(args: argparse.Namespace) -> int:
    if args.full_paths:
        print(
            "Warning: full local paths and vendor locators may reveal user, project, and "
            "repository names; "
            "credential-shaped text remains redacted.",
            file=sys.stderr,
        )
    try:
        with StateStore() as store:
            scan_id = store.latest_scan_id() if args.latest else args.scan_id
            if not scan_id:
                print("No stored scan is available.", file=sys.stderr)
                return 1
            chunks = (
                iter_json_report(store, str(scan_id), redact=not args.full_paths)
                if args.format == "json"
                else iter_markdown_report(store, str(scan_id), redact=not args.full_paths)
            )
            if args.output:
                write_report_stream(args.output, chunks)
                print(f"Report written to {args.output}")
            else:
                for chunk in chunks:
                    sys.stdout.write(chunk)
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def _plan_create(args: argparse.Namespace) -> int:
    selections = tuple(args.select)
    if len(selections) > 256:
        print("A review plan accepts at most 256 explicitly selected candidates.", file=sys.stderr)
        return 2
    if len(set(selections)) != len(selections):
        print("A review plan cannot contain duplicate candidate IDs.", file=sys.stderr)
        return 2
    try:
        with StateStore() as store:
            scan = store.get_scan(args.scan_id)
            if scan is None:
                print(f"Unknown scan: {args.scan_id}", file=sys.stderr)
                return 1
            if scan["status"] != ScanStatus.COMPLETED.value:
                print("Plans require a completed scan.", file=sys.stderr)
                return 1
            records = store.get_resources_by_ids(args.scan_id, selections)
            plan = build_inventory_plan_from_records(args.scan_id, records)
            store.save_inventory_plan(plan)
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1

    payload = {
        "plan": plan.to_dict(),
        "safety_boundary": {
            "executable": False,
            "statement": (
                "This plan contains REPORT_ONLY actions and has no apply or execute path."
            ),
        },
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Report-only plan stored: {plan.plan_id}")
        print(f"  Actions: {len(plan.actions)}; expires: {plan.expires_at.isoformat()}")
        print("  Safety: non-executable; no apply command exists in this milestone.")
    return 0


def _plan_show(args: argparse.Namespace) -> int:
    try:
        with StateStore() as store:
            plan = store.get_inventory_plan(args.plan_id)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    if plan is None:
        print(f"Unknown plan: {args.plan_id}", file=sys.stderr)
        return 1

    payload = {
        "plan": plan,
        "safety_boundary": {
            "executable": False,
            "statement": (
                "Stored inventory plans cannot be imported as execution authority."
            ),
        },
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Report-only plan: {plan['plan_id']}")
        print(f"  Actions: {len(plan['actions'])}; expires: {plan['expires_at']}")
        print("  Safety: non-executable; no apply command exists in this milestone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
